"""Deterministic rules layer.

Pure scoring functions, no LLM. Each rule takes the transaction plus the
customer state accumulated so far in the stream and returns (points, reason_code)
or None. Points sum into a 0-100 risk score, which is banded:

    score < 30    -> auto-approve
    30 <= s < 70  -> triage agent (the ambiguous middle)
    score >= 70   -> auto-decline

with one override: any AML reason code forces the agent band regardless of
score. AML suspicion is never auto-approved and never auto-declined by a
machine; it goes to a human. See README "Design notes".

State is built by replaying the stream in timestamp order, so a rule can only
ever see transactions that already happened. No lookahead.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

APPROVE_BELOW = 30
DECLINE_AT = 70

AML_CODES = {"AML-001", "AML-002"}

RULE_POINTS = {
    "AMT-001": 25,
    "DEV-001": 20,
    "GEO-001": 20,
    "VEL-001": 30,
    "VEL-002": 40,
    "AML-001": 25,
    "AML-002": 40,
    "NEW-001": 25,
    "DOR-001": 15,
}

RULE_DESCRIPTIONS = {
    "AMT-001": "amount over 3x the customer's trailing average",
    "DEV-001": "first-seen device",
    "GEO-001": "country differs from the customer's home country",
    "VEL-001": "more than 5 transactions in 15 minutes",
    "VEL-002": "micro-transaction burst, 8 or more txns under $5 in 15 minutes",
    "AML-001": "amount inside the $9,000-$9,999 structuring band",
    "AML-002": "repeat structuring-band amount to the same beneficiary within 7 days",
    "NEW-001": "account under 7 days old spending over $500",
    "DOR-001": "account dormant 45+ days then reactivated",
    "FBK-001": "heuristic fallback, triage API unavailable",
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class PriorTxn:
    timestamp: datetime
    amount: float
    country: str
    merchant: str
    aml_band: bool


@dataclass
class CustomerState:
    """Everything the rules know about a customer, from the stream so far."""

    priors: list[PriorTxn] = field(default_factory=list)

    def home_country(self) -> str | None:
        if not self.priors:
            return None
        return Counter(p.country for p in self.priors).most_common(1)[0][0]

    def trailing_average(self) -> float | None:
        if len(self.priors) < 3:
            return None
        return sum(p.amount for p in self.priors) / len(self.priors)

    def window(self, now: datetime, minutes: int) -> list[PriorTxn]:
        cutoff = now - timedelta(minutes=minutes)
        return [p for p in self.priors if p.timestamp > cutoff]

    def record(self, txn: dict, aml_band: bool) -> None:
        self.priors.append(PriorTxn(
            timestamp=parse_ts(txn["timestamp"]),
            amount=float(txn["amount"]),
            country=txn["country"],
            merchant=txn["merchant"],
            aml_band=aml_band,
        ))


# --------------------------------------------------------------------------
# Rules. Each returns (points, reason_code) or None.
# --------------------------------------------------------------------------

def rule_amount_spike(txn, state) -> tuple[int, str] | None:
    avg = state.trailing_average()
    if avg is not None and float(txn["amount"]) > 3 * avg:
        return RULE_POINTS["AMT-001"], "AMT-001"
    return None


def rule_new_device(txn, state) -> tuple[int, str] | None:
    if txn["is_new_device"]:
        return RULE_POINTS["DEV-001"], "DEV-001"
    return None


def rule_geo(txn, state) -> tuple[int, str] | None:
    home = state.home_country()
    if home is not None and txn["country"] != home:
        return RULE_POINTS["GEO-001"], "GEO-001"
    return None


def rule_velocity(txn, state) -> tuple[int, str] | None:
    # The current transaction counts toward its own window.
    if len(state.window(parse_ts(txn["timestamp"]), 15)) + 1 > 5:
        return RULE_POINTS["VEL-001"], "VEL-001"
    return None


def rule_micro_burst(txn, state) -> tuple[int, str] | None:
    now = parse_ts(txn["timestamp"])
    micro = [p for p in state.window(now, 15) if p.amount < 5]
    if float(txn["amount"]) < 5:
        micro.append(txn)
    if len(micro) >= 8:
        return RULE_POINTS["VEL-002"], "VEL-002"
    return None


def rule_aml_band(txn, state) -> tuple[int, str] | None:
    if 9000 <= float(txn["amount"]) <= 9999:
        return RULE_POINTS["AML-001"], "AML-001"
    return None


def rule_aml_repeat(txn, state) -> tuple[int, str] | None:
    if not 9000 <= float(txn["amount"]) <= 9999:
        return None
    cutoff = parse_ts(txn["timestamp"]) - timedelta(days=7)
    if any(p.aml_band and p.merchant == txn["merchant"] and p.timestamp > cutoff
           for p in state.priors):
        return RULE_POINTS["AML-002"], "AML-002"
    return None


def rule_new_account(txn, state) -> tuple[int, str] | None:
    if txn["account_age_days"] < 7 and float(txn["amount"]) > 500:
        return RULE_POINTS["NEW-001"], "NEW-001"
    return None


def rule_dormant(txn, state) -> tuple[int, str] | None:
    """Fires on the reactivation itself and anything within 24h of it, so a whole
    burst on a woken-up account carries the flag, not just its first transaction."""
    now = parse_ts(txn["timestamp"])
    stamps = [p.timestamp for p in state.priors] + [now]
    for earlier, later in zip(stamps, stamps[1:]):
        if later - earlier >= timedelta(days=45) and now - later <= timedelta(hours=24):
            return RULE_POINTS["DOR-001"], "DOR-001"
    return None


RULES = [
    rule_amount_spike, rule_new_device, rule_geo, rule_velocity, rule_micro_burst,
    rule_aml_band, rule_aml_repeat, rule_new_account, rule_dormant,
]


@dataclass
class RuleResult:
    score: int
    reason_codes: list[str]
    band: str                 # auto_approve | agent | auto_decline
    aml_override: bool        # band forced to `agent` because AML codes fired


def band_for(score: int, reason_codes: list[str]) -> tuple[str, bool]:
    aml = bool(AML_CODES & set(reason_codes))
    if score < APPROVE_BELOW:
        band = "auto_approve"
    elif score >= DECLINE_AT:
        band = "auto_decline"
    else:
        band = "agent"
    if aml and band != "agent":
        return "agent", True
    return band, False


class RulesEngine:
    """Stateful replay of the stream. Feed transactions in timestamp order."""

    def __init__(self) -> None:
        self.state: dict[str, CustomerState] = {}

    def evaluate(self, txn: dict) -> RuleResult:
        state = self.state.setdefault(txn["customer_id"], CustomerState())
        score = 0
        codes: list[str] = []
        for rule in RULES:
            hit = rule(txn, state)
            if hit:
                points, code = hit
                score += points
                codes.append(code)
        score = min(score, 100)
        band, override = band_for(score, codes)
        state.record(txn, aml_band=9000 <= float(txn["amount"]) <= 9999)
        return RuleResult(score=score, reason_codes=codes, band=band,
                          aml_override=override)
