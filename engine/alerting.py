"""Severity-gated alerting over Resend.

Severity mapping (engine.decisions.severity_for):
    auto-decline, or an agent decline with confidence >= 0.85   -> HIGH
    review carrying an AML reason code                          -> HIGH
    any other review                                            -> MEDIUM
    everything else                                             -> LOG only

Only HIGH sends email, and only once per customer-incident per run. The dedupe
key is customer_id plus typology, so the 13-transaction card-testing burst is
one email, not thirteen. An alert nobody trusts fails the same way an
unactioned one does.

Every alert -- sent, suppressed, or dry-run -- is a row in the `alerts` table.
"""

from __future__ import annotations

import json
import os

from engine.decisions import Decision, log_alert

# reason code -> the typology an analyst would name in the subject line
TYPOLOGY_BY_CODE = [
    # AML-001 and AML-002 are one incident family: a customer repeatedly
    # transacting in the threshold band is one case, not one case per code.
    ({"AML-001"}, "aml-threshold"),
    ({"VEL-002"}, "card-testing"),
    ({"DEV-001", "GEO-001"}, "account-takeover"),
    ({"VEL-001", "DOR-001"}, "velocity-abuse"),
    ({"NEW-001"}, "first-party-abuse"),
    ({"VEL-001"}, "velocity-abuse"),
]

DEFAULT_FROM = "Risk Decisioning Loop <onboarding@resend.dev>"


def typology_for(reason_codes: list[str]) -> str:
    codes = set(reason_codes)
    for required, name in TYPOLOGY_BY_CODE:
        if required <= codes:
            return name
    return "manual-review"


def subject(d: Decision, typology: str) -> str:
    return (f"[{d.severity}] {typology} - {d.txn['customer_id']} "
            f"${d.txn['amount']:,.2f} {d.verdict}")


def body(d: Decision, typology: str) -> str:
    t = d.txn
    conf = f"{d.confidence:.2f}" if d.confidence is not None else "n/a"
    return f"""\
{d.severity} risk alert - {typology}

Verdict        {d.verdict.upper()}   (decided by: {d.source})
Confidence     {conf}
Rules score    {d.score}/100   band: {d.band}{'  [AML override]' if d.aml_override else ''}
Reason codes   {', '.join(d.reason_codes) or '-'}

Transaction
  txn_id       {t['txn_id']}
  customer     {t['customer_id']}
  timestamp    {t['timestamp']}
  amount       {t['amount']:,.2f} {t['currency']}
  merchant     {t['merchant']} (MCC {t['mcc']})
  country      {t['country']}
  device       {t['device_id']}{'  [first seen]' if t['is_new_device'] else ''}
  account age  {t['account_age_days']} days

Analyst narrative
  {d.narrative}

This is one alert per customer-incident for this run; further {typology}
transactions on {t['customer_id']} are suppressed and visible in the review
queue (`python run.py queue`) and the decisions table.
"""


class Alerter:
    def __init__(self, conn, api_key: str | None = None, to: str | None = None,
                 sender: str | None = None, dry_run: bool = False):
        self.conn = conn
        self.to = to or os.getenv("ALERT_TO")
        self.sender = sender or os.getenv("RESEND_FROM") or DEFAULT_FROM
        key = api_key or os.getenv("RESEND_API_KEY")
        self.client = None
        self.reason = ""
        if dry_run:
            self.reason = "dry run"
        elif not key:
            self.reason = "RESEND_API_KEY not set"
        elif not self.to:
            self.reason = "ALERT_TO not set"
        else:
            try:
                import resend
                resend.api_key = key
                self.client = resend
            except Exception as exc:                       # pragma: no cover
                self.reason = f"resend init failed: {exc}"
        self.seen: set[tuple[str, str]] = set()
        self.sent = 0
        self.suppressed = 0
        self.failed = 0
        self.logged_only = 0

    def describe(self) -> str:
        if self.client is None:
            return f"DRY RUN ({self.reason}) - HIGH alerts rendered and logged, not sent"
        return f"Resend -> {self.to} (HIGH only, deduped per customer-incident)"

    def handle(self, d: Decision) -> str:
        if d.severity != "HIGH":
            self.logged_only += 1
            return "log"
        typology = typology_for(d.rule_codes or d.reason_codes)
        key = (d.txn["customer_id"], typology)
        if key in self.seen:
            self.suppressed += 1
            log_alert(self.conn, d.txn["customer_id"], typology, d.txn["txn_id"],
                      d.severity, "suppressed", "deduped: incident already alerted")
            return "suppressed"
        self.seen.add(key)

        if self.client is None:
            log_alert(self.conn, d.txn["customer_id"], typology, d.txn["txn_id"],
                      d.severity, "dry_run", json.dumps({"subject": subject(d, typology)}))
            self.sent += 1
            return "dry_run"
        try:
            resp = self.client.Emails.send({
                "from": self.sender,
                "to": [self.to],
                "subject": subject(d, typology),
                "text": body(d, typology),
            })
            self.sent += 1
            log_alert(self.conn, d.txn["customer_id"], typology, d.txn["txn_id"],
                      d.severity, "sent", json.dumps({"id": resp.get("id"),
                                                      "subject": subject(d, typology)}))
            return "sent"
        except Exception as exc:
            self.failed += 1
            log_alert(self.conn, d.txn["customer_id"], typology, d.txn["txn_id"],
                      d.severity, "failed", f"{type(exc).__name__}: {exc}")
            return "failed"

    def summary_line(self) -> str:
        mode = "sent" if self.client is not None else "rendered (dry run)"
        return (f"[dim]alerts: {self.sent} HIGH {mode}, {self.suppressed} suppressed "
                f"by dedupe, {self.failed} failed, {self.logged_only} logged only[/dim]")
