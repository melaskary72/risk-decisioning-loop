"""Claude triage agent for the ambiguous middle band.

The rules layer handles the extremes. Everything scoring 30-69 (plus anything
carrying an AML code) arrives here, which is the same slice of traffic a human
analyst queue gets today. The agent is given the transaction, the reason codes
that fired and the score, and two read-only tools over the SQLite store:

    get_customer_history(customer_id)          30-day profile, home country,
                                               usual devices, trailing average
    get_related_activity(customer_id, hours)   recent txns plus account events
                                               (device changes, password resets)

Both tools are bounded by the transaction's own timestamp, so the agent cannot
see anything that had not happened yet at decision time.

The agent must reply with one JSON object and nothing else. Parsing is strict:
on malformed JSON we retry once with a corrective turn, then hand off to
engine/fallback.py. Whatever comes back, engine.fallback.enforce_policy has the
last word.
"""

from __future__ import annotations

import json
import os
import time

from engine.decisions import Decision, customer_history, related_activity
from engine.fallback import enforce_policy, heuristic_decision
from engine.rules import RULE_DESCRIPTIONS

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOOL_TURNS = 4

# USD per million tokens (input, output). Cache reads bill at 0.1x input,
# cache writes at 1.25x input.
PRICING = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

SYSTEM_PROMPT = """\
You are a fraud and AML triage analyst at a payments company. A deterministic \
rules engine has already scored this transaction and routed it to you because \
it landed in the ambiguous middle band, where the rules alone cannot tell fraud \
from an unusual but legitimate customer. Your job is to make that call.

WORKFLOW
1. Always call get_customer_history first. A reason code means nothing without \
the customer's baseline: what they normally spend, where they normally spend it, \
which devices they normally use.
2. Always call get_related_activity to see what else happened around this \
transaction. Account events matter as much as transactions: a password change or \
a first-seen device shortly before a large transfer is a takeover signature, \
while a device enrolment that passed an MFA step-up from the home country is a \
customer buying a new phone.
3. Only then decide. Do not decide before calling both tools.

WHAT YOU ARE LOOKING FOR
- Card testing: a burst of tiny authorisations at online merchants, then one \
large purchase.
- Account takeover: new device plus country change plus a credential event, \
followed by rapid transfers that drain the balance.
- Structuring: repeated cash-adjacent transfers just under a $10,000 reporting \
threshold to the same beneficiary.
- Velocity abuse: a dormant account waking up into a rapid burst of purchases.
- First-party abuse: a brand-new account immediately maxing out in a \
chargeback-prone category, with no history to support it.

Weigh the exculpatory evidence just as hard. A traveller on a known device, a \
tuition payment to a university bursar, a seasonal customer returning after a \
quiet period, and a customer's first big purchase on a legitimately enrolled \
device all look risky to the rules and are not fraud. A false decline costs a \
real customer.

DECISION RULES
- Return "review" whenever your confidence is below 0.7. An unsure verdict is an \
escalation, not a decision.
- Return "review", never "decline", whenever an AML reason code (AML-001, \
AML-002) fired. Structuring suspicion is a filing question for a human \
compliance analyst; a model does not auto-decline it.
- A transaction that is one of a still-developing rapid sequence on an account the rules have already flagged -- a reactivated dormant account, a velocity flag, a brand-new account -- is not explained just because that single transaction looks ordinary in isolation. A burst is often only recognisable in hindsight, and the second transaction of one looks exactly like the second transaction of a normal afternoon. Where the history cannot positively explain the sequence, return "review" rather than "approve".
- Use "decline" only when the evidence you actually retrieved supports it.
- Use "approve" only when the history explains the transaction.
- reason_codes: return the codes that genuinely drive your verdict. Reuse the \
codes that fired where they apply; you may drop one the history explains away.
- risk_narrative: one to three sentences, written for the analyst who picks this \
up. Cite the specific evidence you found, not the rule names.

OUTPUT
Reply with exactly one JSON object and nothing else. No prose before or after, \
no markdown fence.

{"verdict": "approve|decline|review", "reason_codes": ["GEO-001"], \
"risk_narrative": "...", "confidence": 0.0}
"""

TOOLS = [
    {
        "name": "get_customer_history",
        "description": (
            "30-day and all-time summary for a customer as of this transaction: "
            "transaction counts and totals, home country and country mix, usual "
            "devices, trailing average and largest historical amount, and the "
            "five most recent prior transactions."),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string",
                                "description": "e.g. CUST-017"},
            },
            "required": ["customer_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_related_activity",
        "description": (
            "Everything on the account in the N hours before this transaction: "
            "the transactions themselves and account events such as device "
            "changes and password changes."),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "window_hours": {
                    "type": "integer",
                    "description": "Lookback window in hours, e.g. 24 or 168."},
            },
            "required": ["customer_id", "window_hours"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

VERDICTS = {"approve", "decline", "review"}


class MalformedVerdict(Exception):
    pass


def build_user_message(txn: dict, result) -> str:
    fired = "\n".join(f"  - {c}: {RULE_DESCRIPTIONS[c]}" for c in result.reason_codes)
    override = ("\nThis transaction reached you via the AML override: any AML "
                "reason code is routed here regardless of score.\n"
                if result.aml_override else "")
    return (
        f"Transaction under triage:\n```json\n{json.dumps(txn, indent=2)}\n```\n"
        f"Rules score: {result.score}/100 (band 30-69 goes to triage)\n"
        f"Reason codes that fired:\n{fired or '  (none)'}\n{override}\n"
        f"Investigate with the tools, then return your JSON verdict."
    )


def parse_verdict(text: str) -> dict:
    """Strict parse. One JSON object, the documented keys, sane values."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.lower().startswith("json") else raw
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedVerdict(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedVerdict("top level is not an object")
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        raise MalformedVerdict(f"verdict {verdict!r} not one of {sorted(VERDICTS)}")
    codes = obj.get("reason_codes")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise MalformedVerdict("reason_codes must be a list of strings")
    narrative = obj.get("risk_narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        raise MalformedVerdict("risk_narrative must be a non-empty string")
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MalformedVerdict("confidence must be a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise MalformedVerdict(f"confidence {confidence} outside 0.0-1.0")
    return {"verdict": verdict, "reason_codes": codes,
            "risk_narrative": narrative.strip(), "confidence": confidence}


def cost_usd(model: str, usage) -> float:
    in_rate, out_rate = PRICING.get(model, PRICING[DEFAULT_MODEL])
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (usage.input_tokens * in_rate
            + cache_read * in_rate * 0.1
            + cache_write * in_rate * 1.25
            + usage.output_tokens * out_rate) / 1_000_000


class TriageAgent:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.client = None
        self.disabled_reason = "ANTHROPIC_API_KEY not set"
        if key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=90.0)
                self.disabled_reason = ""
            except Exception as exc:                       # pragma: no cover
                self.disabled_reason = f"client init failed: {exc}"
        self.calls = 0
        self.fallbacks = 0
        self.retries = 0

    def describe(self) -> str:
        if self.client is None:
            return f"DISABLED ({self.disabled_reason}) - heuristic fallback in use"
        return f"{self.model} with 2 tools, strict JSON parse"

    # -- tool dispatch ----------------------------------------------------
    def _run_tool(self, conn, name: str, args: dict, as_of: str) -> str:
        if name == "get_customer_history":
            payload = customer_history(conn, args["customer_id"], as_of)
        elif name == "get_related_activity":
            hours = int(args.get("window_hours", 24))
            payload = related_activity(conn, args["customer_id"], as_of,
                                       max(1, min(hours, 24 * 90)))
        else:
            return json.dumps({"error": f"unknown tool {name}"})
        return json.dumps(payload, default=str)

    # -- main entry point -------------------------------------------------
    def triage(self, txn: dict, result, conn) -> Decision:
        if self.client is None:
            self.fallbacks += 1
            return heuristic_decision(txn, result, self.disabled_reason)

        started = time.perf_counter()
        usage_in = usage_out = 0
        cost = 0.0
        messages = [{"role": "user", "content": build_user_message(txn, result)}]
        tool_calls: list[str] = []
        parse_attempts = 0

        try:
            for _ in range(MAX_TOOL_TURNS + 2):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1500,
                    system=[{"type": "text", "text": SYSTEM_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=messages,
                )
                usage_in += response.usage.input_tokens
                usage_out += response.usage.output_tokens
                cost += cost_usd(self.model, response.usage)

                if response.stop_reason == "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        tool_calls.append(block.name)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": self._run_tool(conn, block.name, block.input,
                                                      txn["timestamp"]),
                        })
                    messages.append({"role": "user", "content": results})
                    continue

                text = "".join(b.text for b in response.content if b.type == "text")
                try:
                    parsed = parse_verdict(text)
                except MalformedVerdict as exc:
                    parse_attempts += 1
                    if parse_attempts > 1:
                        raise
                    self.retries += 1
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content":
                        f"That was not a valid verdict ({exc}). Reply with exactly "
                        f"one JSON object with keys verdict, reason_codes, "
                        f"risk_narrative, confidence. No other text."})
                    continue
                break
            else:
                raise MalformedVerdict("no verdict within the turn budget")
        except Exception as exc:
            self.fallbacks += 1
            d = heuristic_decision(txn, result, f"{type(exc).__name__}: {exc}")
            d.latency_ms = int((time.perf_counter() - started) * 1000)
            d.input_tokens, d.output_tokens, d.cost_usd = usage_in, usage_out, cost
            d.tool_calls = tool_calls
            return d

        self.calls += 1
        d = Decision(
            txn=txn, score=result.score, band=result.band,
            aml_override=result.aml_override,
            verdict=parsed["verdict"],
            reason_codes=parsed["reason_codes"],
            rule_codes=result.reason_codes,
            narrative=parsed["risk_narrative"],
            source="agent",
            confidence=parsed["confidence"],
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=usage_in, output_tokens=usage_out, cost_usd=cost,
            tool_calls=tool_calls,
        )
        return enforce_policy(d)
