"""Decision records and the SQLite state store.

Every decision the loop makes -- rules, agent, or fallback -- is one row in
`decisions`. That table *is* the audit trail: score, band, verdict, the reason
codes that fired, the narrative an analyst reads, latency, tokens and cost, and
which layer produced it. Nothing decides anything without leaving a row.

`review_queue` holds the exceptions routed to a human. `alerts` records every
severity-gated notification (and every one suppressed by dedupe).

The `transactions` and `events` tables are the read side for the triage agent's
tools; they are loaded from the same JSONL the stream replays, with the `label`
column dropped so the engine can never see ground truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "risk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id           TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    amount           REAL NOT NULL,
    currency         TEXT NOT NULL,
    merchant         TEXT NOT NULL,
    mcc              TEXT NOT NULL,
    country          TEXT NOT NULL,
    device_id        TEXT NOT NULL,
    is_new_device    INTEGER NOT NULL,
    account_age_days INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_cust ON transactions(customer_id, timestamp);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evt_cust ON events(customer_id, timestamp);

CREATE TABLE IF NOT EXISTS decisions (
    txn_id        TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    amount        REAL NOT NULL,
    merchant      TEXT NOT NULL,
    mcc           TEXT NOT NULL,
    country       TEXT NOT NULL,
    score         INTEGER NOT NULL,
    band          TEXT NOT NULL,
    aml_override  INTEGER NOT NULL DEFAULT 0,
    verdict       TEXT NOT NULL,
    reason_codes  TEXT NOT NULL,
    rule_codes    TEXT NOT NULL DEFAULT '[]',
    narrative     TEXT NOT NULL,
    confidence    REAL,
    severity      TEXT NOT NULL,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    source        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id       TEXT NOT NULL UNIQUE,
    customer_id  TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    severity     TEXT NOT NULL,
    score        INTEGER NOT NULL,
    amount       REAL NOT NULL,
    reason_codes TEXT NOT NULL,
    narrative    TEXT NOT NULL,
    source       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL,
    incident_key TEXT NOT NULL,
    txn_id       TEXT NOT NULL,
    severity     TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NOT NULL
);
"""


@dataclass
class Decision:
    """One decision record. Written verbatim to `decisions`."""

    txn: dict
    score: int
    band: str
    aml_override: bool
    verdict: str                      # approve | decline | review
    reason_codes: list[str]           # codes the DECIDER stands behind
    rule_codes: list[str]             # codes the RULES fired; guardrails use these
    narrative: str
    source: str                       # rules | agent | fallback
    confidence: float | None = None
    severity: str = "LOG"             # HIGH | MEDIUM | LOG
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: list[str] = field(default_factory=list)


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def reset(conn: sqlite3.Connection) -> None:
    """Fresh run: clear decisions, queue and alerts, keep the loaded stream."""
    conn.executescript(
        "DELETE FROM decisions; DELETE FROM review_queue; DELETE FROM alerts;")
    conn.commit()


ENGINE_FIELDS = ["txn_id", "customer_id", "timestamp", "amount", "currency",
                 "merchant", "mcc", "country", "device_id", "is_new_device",
                 "account_age_days"]


def strip_labels(raw: dict) -> dict:
    """Hand the engine only the fields a production system would have."""
    return {k: raw[k] for k in ENGINE_FIELDS}


def load_stream(conn: sqlite3.Connection, txn_path: Path, event_path: Path) -> list[dict]:
    """Load JSONL into SQLite (labels dropped) and return the labelled stream."""
    raw = [json.loads(line) for line in txn_path.read_text().splitlines() if line]
    raw.sort(key=lambda t: t["timestamp"])
    events = []
    if event_path.exists():
        events = [json.loads(l) for l in event_path.read_text().splitlines() if l]

    conn.executescript("DELETE FROM transactions; DELETE FROM events;")
    conn.executemany(
        f"INSERT INTO transactions ({','.join(ENGINE_FIELDS)}) "
        f"VALUES ({','.join('?' * len(ENGINE_FIELDS))})",
        [tuple(strip_labels(t)[k] for k in ENGINE_FIELDS) for t in raw])
    conn.executemany(
        "INSERT INTO events (event_id, customer_id, timestamp, event_type, detail) "
        "VALUES (?,?,?,?,?)",
        [(e["event_id"], e["customer_id"], e["timestamp"], e["event_type"], e["detail"])
         for e in events])
    conn.commit()
    return raw


def record(conn: sqlite3.Connection, d: Decision) -> None:
    t = d.txn
    conn.execute(
        """INSERT OR REPLACE INTO decisions (
             txn_id, customer_id, timestamp, amount, merchant, mcc, country,
             score, band, aml_override, verdict, reason_codes, rule_codes,
             narrative, confidence, severity, latency_ms, input_tokens,
             output_tokens, cost_usd, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["txn_id"], t["customer_id"], t["timestamp"], t["amount"], t["merchant"],
         t["mcc"], t["country"], d.score, d.band, int(d.aml_override), d.verdict,
         json.dumps(d.reason_codes), json.dumps(d.rule_codes),
         d.narrative, d.confidence, d.severity,
         d.latency_ms, d.input_tokens, d.output_tokens, d.cost_usd, d.source))
    if d.verdict == "review":
        conn.execute(
            """INSERT OR REPLACE INTO review_queue (
                 txn_id, customer_id, opened_at, severity, score, amount,
                 reason_codes, narrative, source, status)
               VALUES (?,?,?,?,?,?,?,?,?,'open')""",
            (t["txn_id"], t["customer_id"], t["timestamp"], d.severity, d.score,
             t["amount"], json.dumps(d.reason_codes), d.narrative, d.source))
    conn.commit()


def log_alert(conn, customer_id: str, incident_key: str, txn_id: str,
              severity: str, status: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO alerts (customer_id, incident_key, txn_id, severity, status, detail)"
        " VALUES (?,?,?,?,?,?)",
        (customer_id, incident_key, txn_id, severity, status, detail))
    conn.commit()


def open_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_queue WHERE status='open' "
        "ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,"
        " opened_at").fetchall()


# --------------------------------------------------------------------------
# Read side for the triage agent's tools. Both are bounded by `as_of` so the
# agent can never see a transaction that has not happened yet.
# --------------------------------------------------------------------------

def customer_history(conn, customer_id: str, as_of: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM transactions WHERE customer_id=? AND timestamp < ? "
        "ORDER BY timestamp", (customer_id, as_of)).fetchall()
    if not rows:
        return {"customer_id": customer_id, "txn_count_all_time": 0,
                "note": "no prior transactions on this account"}
    recent = [r for r in rows if r["timestamp"] >= _minus_days(as_of, 30)]
    amounts = [r["amount"] for r in rows]
    countries: dict[str, int] = {}
    devices: dict[str, int] = {}
    for r in rows:
        countries[r["country"]] = countries.get(r["country"], 0) + 1
        devices[r["device_id"]] = devices.get(r["device_id"], 0) + 1
    home = max(countries, key=countries.get)
    return {
        "customer_id": customer_id,
        "txn_count_all_time": len(rows),
        "first_seen": rows[0]["timestamp"],
        "account_age_days": rows[-1]["account_age_days"],
        "home_country": home,
        "country_mix": countries,
        "usual_devices": sorted(devices, key=devices.get, reverse=True)[:3],
        "device_counts": devices,
        "trailing_average_amount": round(sum(amounts) / len(amounts), 2),
        "max_amount_all_time": round(max(amounts), 2),
        "last_30d": {
            "txn_count": len(recent),
            "total_amount": round(sum(r["amount"] for r in recent), 2),
            "average_amount": round(sum(r["amount"] for r in recent) / len(recent), 2)
            if recent else 0.0,
            "distinct_merchants": len({r["merchant"] for r in recent}),
            "top_mccs": sorted({r["mcc"] for r in recent})[:6],
        },
        "last_5_txns": [
            {"timestamp": r["timestamp"], "amount": r["amount"],
             "merchant": r["merchant"], "mcc": r["mcc"], "country": r["country"],
             "device_id": r["device_id"]}
            for r in rows[-5:]],
    }


def related_activity(conn, customer_id: str, as_of: str, window_hours: int) -> dict:
    start = _minus_hours(as_of, window_hours)
    txns = conn.execute(
        "SELECT * FROM transactions WHERE customer_id=? AND timestamp >= ? "
        "AND timestamp < ? ORDER BY timestamp",
        (customer_id, start, as_of)).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE customer_id=? AND timestamp >= ? "
        "AND timestamp < ? ORDER BY timestamp",
        (customer_id, start, as_of)).fetchall()
    return {
        "customer_id": customer_id,
        "window_hours": window_hours,
        "window_start": start,
        "window_end": as_of,
        "txn_count": len(txns),
        "total_amount": round(sum(r["amount"] for r in txns), 2),
        "transactions": [
            {"timestamp": r["timestamp"], "amount": r["amount"],
             "merchant": r["merchant"], "mcc": r["mcc"], "country": r["country"],
             "device_id": r["device_id"], "is_new_device": bool(r["is_new_device"])}
            for r in txns[-25:]],
        "account_events": [
            {"timestamp": r["timestamp"], "event_type": r["event_type"],
             "detail": r["detail"]} for r in events],
    }


def _minus_days(iso: str, days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(iso) - timedelta(days=days)).isoformat(timespec="seconds")


def _minus_hours(iso: str, hours: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(iso) - timedelta(hours=hours)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Severity is a property of the decision record; engine/alerting.py decides
# what to do about it.
# --------------------------------------------------------------------------

from engine.rules import AML_CODES  # noqa: E402


def severity_for(d: "Decision") -> str:
    """auto-decline, or a confident agent decline -> HIGH.
    review carrying an AML code -> HIGH. Other reviews -> MEDIUM. Rest -> LOG."""
    if d.verdict == "decline":
        if d.source == "rules":
            return "HIGH"
        if (d.confidence or 0.0) >= 0.85:
            return "HIGH"
        return "MEDIUM"
    if d.verdict == "review":
        if AML_CODES & set(d.rule_codes) | AML_CODES & set(d.reason_codes):
            return "HIGH"
        return "MEDIUM"
    return "LOG"
