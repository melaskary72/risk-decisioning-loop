#!/usr/bin/env python3
"""Stream simulator: generate -> decide -> route.

Replays data/transactions.jsonl in timestamp order. For each transaction:
deterministic rules, band, the Claude triage agent if the score lands in the
ambiguous middle, persist the decision, then severity-gated alerting.

    python run.py                 full run
    python run.py --no-agent      rules only; the middle band routes to review
    python run.py --no-alerts     decide and persist, send nothing
    python run.py queue           print the open human-review queue
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import decisions as store          # noqa: E402
from engine.decisions import Decision          # noqa: E402
from engine.rules import RULE_DESCRIPTIONS, RulesEngine  # noqa: E402

TXN_PATH = ROOT / "data" / "transactions.jsonl"
EVENT_PATH = ROOT / "data" / "events.jsonl"

VERDICT_STYLE = {"approve": "green", "decline": "bold red", "review": "yellow"}
SOURCE_STYLE = {"rules": "dim", "agent": "cyan", "fallback": "magenta"}
console = Console()


def rules_narrative(result) -> str:
    if not result.reason_codes:
        return "No rules fired; score below the auto-approve threshold."
    detail = "; ".join(f"{c} ({RULE_DESCRIPTIONS[c]})" for c in result.reason_codes)
    return f"Rules score {result.score}: {detail}."


def decide(txn, rules, agent, conn) -> Decision:
    result = rules.evaluate(txn)
    if result.band == "agent" and agent is not None:
        d = agent.triage(txn, result, conn)
    elif result.band == "agent":
        # --no-agent: the middle band is exactly what a human would look at.
        d = Decision(txn=txn, score=result.score, band=result.band,
                     aml_override=result.aml_override, verdict="review",
                     reason_codes=result.reason_codes,
                     rule_codes=result.reason_codes,
                     narrative=rules_narrative(result) + " Routed to review (agent disabled).",
                     source="rules")
    else:
        verdict = "approve" if result.band == "auto_approve" else "decline"
        d = Decision(txn=txn, score=result.score, band=result.band,
                     aml_override=result.aml_override, verdict=verdict,
                     reason_codes=result.reason_codes,
                     rule_codes=result.reason_codes,
                     narrative=rules_narrative(result), source="rules")
    d.severity = store.severity_for(d)
    store.record(conn, d)
    return d


def line(d: Decision) -> None:
    t = d.txn
    codes = ",".join(d.reason_codes) or "-"
    conf = f"{d.confidence:.2f}" if d.confidence is not None else "  - "
    console.print(
        f"[dim]{t['timestamp']}[/dim] {t['txn_id']} {t['customer_id']} "
        f"[white]${t['amount']:>9,.2f}[/white] {t['merchant'][:26]:<26} "
        f"score=[bold]{d.score:>3}[/bold] "
        f"[{VERDICT_STYLE[d.verdict]}]{d.verdict.upper():<8}[/] "
        f"[{SOURCE_STYLE[d.source]}]{d.source:<8}[/] conf={conf} "
        f"[dim]{codes}[/dim]")


def summary(ds: list[Decision], wall_s: float) -> None:
    bands = {b: sum(1 for d in ds if d.band == b)
             for b in ("auto_approve", "agent", "auto_decline")}
    verdicts = {v: sum(1 for d in ds if d.verdict == v)
                for v in ("approve", "decline", "review")}
    agent_ds = [d for d in ds if d.source == "agent"]
    fallbacks = [d for d in ds if d.source == "fallback"]
    cost = sum(d.cost_usd for d in ds)
    lat = [d.latency_ms for d in ds if d.source in ("agent", "fallback") and d.latency_ms]
    in_tok = sum(d.input_tokens for d in ds)
    out_tok = sum(d.output_tokens for d in ds)

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(style="bold")
    t.add_row("transactions", f"{len(ds)}")
    t.add_row("", "")
    t.add_row("auto-approve (score < 30)", f"{bands['auto_approve']}")
    t.add_row("agent band (30-69, + AML override)", f"{bands['agent']}")
    t.add_row("auto-decline (score >= 70)", f"{bands['auto_decline']}")
    t.add_row("", "")
    t.add_row("approved", f"[green]{verdicts['approve']}[/green]")
    t.add_row("declined", f"[bold red]{verdicts['decline']}[/bold red]")
    t.add_row("routed to human review", f"[yellow]{verdicts['review']}[/yellow]")
    t.add_row("", "")
    t.add_row("agent calls", f"{len(agent_ds)}")
    t.add_row("heuristic fallbacks", f"{len(fallbacks)}")
    t.add_row("agent tokens (in / out)", f"{in_tok:,} / {out_tok:,}")
    t.add_row("agent cost this run", f"${cost:.4f}")
    if agent_ds:
        per_1k = cost / len(agent_ds) * 1000
        t.add_row("cost per 1,000 agent decisions", f"${per_1k:.2f}")
    if lat:
        t.add_row("p50 triage latency", f"{statistics.median(lat):,.0f} ms")
        t.add_row("p95 triage latency",
                  f"{sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]:,.0f} ms")
    t.add_row("wall clock", f"{wall_s:,.1f} s")
    console.print()
    console.print(Panel(t, title="[bold]decision stream summary[/bold]",
                        border_style="cyan", expand=False))


def show_queue(conn) -> None:
    rows = store.open_queue(conn)
    table = Table(title=f"open review queue ({len(rows)})",
                  header_style="bold", border_style="yellow")
    table.add_column("txn", style="dim")
    table.add_column("customer")
    table.add_column("sev")
    table.add_column("score", justify="right")
    table.add_column("amount", justify="right")
    table.add_column("codes")
    table.add_column("src")
    table.add_column("narrative", max_width=62)
    for r in rows:
        sev = r["severity"]
        table.add_row(
            r["txn_id"], r["customer_id"],
            f"[red]{sev}[/red]" if sev == "HIGH" else f"[yellow]{sev}[/yellow]",
            str(r["score"]), f"${r['amount']:,.2f}",
            ",".join(json.loads(r["reason_codes"])) or "-",
            r["source"], r["narrative"])
    console.print(table)


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "queue"])
    ap.add_argument("--no-agent", action="store_true",
                    help="skip the LLM; the middle band routes straight to review")
    ap.add_argument("--no-alerts", action="store_true", help="do not send email alerts")
    ap.add_argument("--dry-run-alerts", action="store_true",
                    help="render and log HIGH alerts without sending")
    ap.add_argument("--only-flagged", action="store_true",
                    help="print only decline/review lines")
    ap.add_argument("--limit", type=int, help="replay only the first N transactions")
    ap.add_argument("--db", default=str(store.DB_PATH))
    args = ap.parse_args()

    conn = store.connect(args.db)
    if args.command == "queue":
        show_queue(conn)
        return 0

    if not TXN_PATH.exists():
        console.print("[red]data/transactions.jsonl not found. "
                      "Run `python data/generate.py` first.[/red]")
        return 1

    labelled = store.load_stream(conn, TXN_PATH, EVENT_PATH)
    store.reset(conn)
    stream = [store.strip_labels(t) for t in labelled]
    if args.limit:
        stream = stream[:args.limit]

    agent = None
    if not args.no_agent:
        from engine import triage_agent
        agent = triage_agent.TriageAgent()
        console.print(f"[dim]triage agent: {agent.describe()}[/dim]")

    alerter = None
    if not args.no_alerts:
        from engine import alerting
        alerter = alerting.Alerter(conn, dry_run=args.dry_run_alerts)
        console.print(f"[dim]alerting: {alerter.describe()}[/dim]")

    console.rule(f"[bold]replaying {len(stream)} transactions[/bold]")
    rules = RulesEngine()
    out: list[Decision] = []
    t0 = time.time()
    for txn in stream:
        d = decide(txn, rules, agent, conn)
        out.append(d)
        if not args.only_flagged or d.verdict != "approve":
            line(d)
        if alerter is not None:
            alerter.handle(d)
    wall = time.time() - t0

    summary(out, wall)
    if alerter is not None:
        console.print(alerter.summary_line())
    console.print(f"[dim]audit trail: {args.db} "
                  f"(decisions, review_queue, alerts) - `python run.py queue`[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
