"""Deterministic policy: what happens when the model is unavailable, and the
compliance guardrails that hold whatever the model says.

Two separate things live here, both LLM-free on purpose:

`heuristic_decision` is the fallback. If the Anthropic API errors, or the agent
returns unparseable JSON twice, the loop still completes: score >= 55 becomes
`review`, anything lower is approved, tagged FBK-001. Every fallback is logged.

`enforce_policy` runs on *every* agent and fallback verdict. It is the part of
the system that does not trust the model: AML suspicion is never auto-approved
or auto-declined by a machine, and a low-confidence verdict is not a verdict.
"""

from __future__ import annotations

import logging
from pathlib import Path

from engine.decisions import Decision
from engine.rules import AML_CODES

FALLBACK_AT = 55
MIN_CONFIDENCE = 0.7
LOG_PATH = Path(__file__).resolve().parent.parent / "fallbacks.log"

log = logging.getLogger("risk.fallback")
if not log.handlers:
    log.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


def heuristic_decision(txn: dict, result, reason: str) -> Decision:
    """The 'still safe when the model is down' path."""
    verdict = "review" if result.score >= FALLBACK_AT else "approve"
    d = Decision(
        txn=txn,
        score=result.score,
        band=result.band,
        aml_override=result.aml_override,
        verdict=verdict,
        reason_codes=result.reason_codes + ["FBK-001"],
        rule_codes=result.reason_codes,
        narrative="heuristic fallback, API unavailable",
        source="fallback",
        confidence=None,
    )
    d = enforce_policy(d)
    log.info("FBK-001 txn=%s customer=%s score=%s verdict=%s reason=%s",
             txn["txn_id"], txn["customer_id"], result.score, d.verdict, reason)
    return d


def enforce_policy(d: Decision) -> Decision:
    """Guardrails applied after the model (or the heuristic) has spoken.

    Both checks read `rule_codes` -- the codes the deterministic layer fired --
    never the codes the model chose to return. A guardrail that trusts the output
    it is guarding is not a guardrail: the agent is free to drop a code it thinks
    it explained away, and the AML guarantee must survive that.

    1. Any AML reason code -> human review. Never machine-approved, never
       machine-declined. Structuring is a SAR question, not a decline button.
    2. Confidence below 0.7 -> human review. An unsure model is an escalation.
    """
    notes = []
    if (AML_CODES & (set(d.rule_codes) | set(d.reason_codes))
            and d.verdict != "review"):
        notes.append(f"Policy override: AML code present, {d.verdict} -> review "
                     "(AML suspicion always goes to a human).")
        d.verdict = "review"
    if (d.confidence is not None and d.confidence < MIN_CONFIDENCE
            and d.verdict != "review"):
        notes.append(f"Policy override: confidence {d.confidence:.2f} below "
                     f"{MIN_CONFIDENCE}, {d.verdict} -> review.")
        d.verdict = "review"
    if notes:
        d.narrative = f"{d.narrative} {' '.join(notes)}"
        log.info("POLICY txn=%s %s", d.txn["txn_id"], " ".join(notes))
    return d
