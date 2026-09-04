# risk-decisioning-loop · Build Spec

Demo project for the Oscilar Forward Deployed Engineer application. A miniature AI risk decisioning engine over a transaction stream: deterministic rules first, a Claude triage agent for the ambiguous middle band, human-review routing for exceptions, severity-gated alerting, and evals against seeded ground truth. Same architecture family as lookalike-domain-monitor, so most patterns port directly.

**Repo name:** `risk-decisioning-loop`
**Repo description:** Mini AI risk decisioning engine: deterministic rules, Claude triage agent with evals and heuristic fallback, human-review routing, severity-gated alerts.
**Topics:** `fraud-detection`, `risk-decisioning`, `llm-agents`, `anthropic`, `fintech`, `aml`
**Stack:** Python 3.11+, Anthropic API (Claude), SQLite, Resend, no framework. CLI plus README screenshots, no web UI.
**Build budget:** one afternoon. Cut scope before cutting the eval.

---

## 1. Why this shape (the story for outreach)

Oscilar's product is rules plus AI agents plus human analysts making onboarding, fraud, credit, and AML decisions. This demo mirrors that exact shape end to end at miniature scale, and covers two of their four pillars: transaction fraud and an AML structuring pattern. The one-line pitch: "I built a miniature version of your product's decision loop the same afternoon I applied."

## 2. Repo layout

```
risk-decisioning-loop/
├── README.md
├── .env.example              # ANTHROPIC_API_KEY, RESEND_API_KEY, ALERT_TO
├── requirements.txt          # anthropic, resend, rich, python-dotenv
├── data/
│   └── generate.py           # synthetic txn generator with seeded fraud
├── engine/
│   ├── rules.py              # deterministic rules layer, risk score
│   ├── triage_agent.py       # Claude agent for the ambiguous band
│   ├── fallback.py           # heuristic fallback when the API fails
│   ├── decisions.py          # decision records, SQLite state store
│   └── alerting.py           # severity-gated Resend alerts
├── run.py                    # stream simulator: generate -> decide -> route
├── evals/
│   ├── run_evals.py          # scores decisions against seeded ground truth
│   └── results.md            # committed output of the eval run
└── docs/
    └── screenshots/          # CLI run, alert email, eval table
```

## 3. Data: synthetic transactions with seeded ground truth

Do not download a Kaggle dataset. Generate synthetic data so ground truth is exact and evals are honest (same trick as the seeded bugs in pr-review-loop).

`data/generate.py` produces ~500 transactions across ~40 customers as JSONL. Each txn: `txn_id, customer_id, timestamp, amount, currency, merchant, mcc, country, device_id, is_new_device, account_age_days, label` (label is hidden from the engine, used only by evals).

~92% legitimate traffic (groceries, gas, subscriptions, salary-scale transfers, normal geo/device continuity). Seed exactly these five typologies, tagged in `label`:

1. **Card testing:** one customer, 12 txns of $0.50 to $3 at online merchants inside 10 minutes, then a $900 attempt.
2. **Account takeover:** new device plus country jump plus password-change event, followed by three rapid transfers draining ~80% of balance.
3. **Structuring (AML):** six cash-adjacent transfers of $9,100 to $9,800 across four days, same beneficiary.
4. **Velocity spike:** dormant account (no activity 60 days) suddenly does 9 purchases in one hour.
5. **First-party abuse:** brand-new account (age < 2 days) making a max-limit purchase with immediate chargeback-prone merchant category.

Also seed 4 or 5 **legitimate look-alikes** (a traveler making foreign purchases on a known device, a legitimate $9,500 tuition payment) so false positives are measurable and the agent has real work to do.

## 4. Rules layer (`engine/rules.py`)

Pure functions, no LLM. Each rule returns `(points, reason_code)`. Sum points into a risk score 0 to 100.

| Rule | Reason code | Points |
|---|---|---|
| Amount > 3x customer's trailing average | `AMT-001` | 25 |
| New device | `DEV-001` | 20 |
| Country differs from home country | `GEO-001` | 20 |
| >5 txns in 15 minutes | `VEL-001` | 30 |
| Micro-txn burst (>=8 txns under $5 in 15 min) | `VEL-002` | 40 |
| Amount in $9,000 to $9,999 band | `AML-001` | 25 |
| Repeat AML-001 hits within 7 days, same beneficiary | `AML-002` | 40 |
| Account age < 7 days and amount > $500 | `NEW-001` | 25 |
| Dormant 45+ days then active | `DOR-001` | 15 |

Banding: **score < 30 auto-approve, score >= 70 auto-decline, 30 to 69 goes to the agent.** State the design point in the README: rules give speed and auditability at the extremes, the agent spends judgment only on the ambiguous middle, which is exactly where analyst time goes today.

## 5. Triage agent (`engine/triage_agent.py`)

Claude (`claude-sonnet-4-6` via API) with tool use. Input: the transaction, its fired reason codes and score. Tools (both read from SQLite, built from the same JSONL):

- `get_customer_history(customer_id)` returns 30-day txn summary, home country, usual devices, trailing average.
- `get_related_activity(customer_id, window_hours)` returns recent txns and events (device changes, password changes) in the window.

System prompt requirements: act as a fraud/AML triage analyst; call tools before deciding; return **only** this JSON:

```json
{
  "verdict": "approve | decline | review",
  "reason_codes": ["GEO-001"],
  "risk_narrative": "one to three sentences an analyst can read",
  "confidence": 0.0
}
```

Rules of the prompt: `review` is mandatory when confidence < 0.7 or when AML codes fired (AML suspicion goes to a human, never auto-declined by a model, a compliance-realistic touch worth one README sentence). Parse strictly; on malformed JSON retry once, then fall back.

**Fallback (`engine/fallback.py`):** if the API errors or both parses fail, deterministic policy: score >= 55 becomes `review`, else `approve`, reason code `FBK-001`, narrative "heuristic fallback, API unavailable". Log every fallback. This is the "still safe when the model is down" line for the README.

## 6. Decisions, routing, alerting

`engine/decisions.py`: every decision (auto or agent) is a row in SQLite: txn, score, band, verdict, reason codes, narrative, latency_ms, tokens, source (`rules | agent | fallback`). The table **is** the audit trail; say so in the README.

Routing: `review` verdicts land in a `review_queue` table with status `open`. Add a tiny CLI `python run.py queue` that prints the open queue as a rich table (screenshot this).

`engine/alerting.py`: severity mapping: auto-decline or agent decline with confidence >= 0.85 is HIGH, `review` with AML codes is HIGH, other reviews MEDIUM, everything else logged only. HIGH sends one Resend email per customer-incident (dedupe by customer_id plus typology within the run) with txn details, codes, and the narrative. Screenshot one alert email.

## 7. `run.py` (the demo run)

Replays the JSONL in timestamp order as a stream. Per txn: rules, band, agent if middle band, persist, alert. Rich console output, one line per decision with colored verdicts, then a summary block: counts per band, agent calls, fallbacks, total cost estimate, p50 latency. Screenshot the summary.

## 8. Evals (`evals/run_evals.py`)

Join decisions against hidden labels. Report, and commit to `evals/results.md`:

- **Fraud recall:** % of seeded fraud txns ending in decline or review (target: 5/5 typologies caught, 100% of fraudulent txns not auto-approved).
- **False positive rate:** % of legitimate txns declined (target: 0 declines; look-alikes may go to review, report that number honestly).
- **Agent verdict accuracy** on the middle band vs labels.
- **AML routing correctness:** 100% of AML-code txns routed to human review, never auto-declined.
- **Fallback correctness:** run once with the API key removed to prove the loop completes on heuristics.
- **Latency and cost:** median agent decision time, tokens, $ per 1,000 decisions.

These numbers go verbatim into the README table and the outreach email, exactly like the 5/5 and 0-false-positive lines from pr-review-loop.

## 9. README structure

1. One-paragraph pitch plus an architecture line: `stream -> rules -> {approve | decline | agent} -> {approve | decline | review queue} -> alerts`, with a simple ASCII or image diagram.
2. Screenshot: decision stream summary.
3. Eval results table plus one sentence per number.
4. Design notes (five short bullets): rules at the extremes and judgment in the middle; AML always routes to a human; heuristic fallback; the decision table as audit trail; alert dedupe because an alert nobody trusts fails the same as an unactioned one.
5. Quickstart: `pip install -r requirements.txt`, `.env`, `python data/generate.py`, `python run.py`, `python evals/run_evals.py`.
6. Honest scope note: synthetic data, miniature by design, built in an afternoon to mirror the shape of production risk decisioning.

## 10. Build order (afternoon plan)

1. `generate.py` plus rules layer, run end to end with bands only (no agent). ~45 min.
2. SQLite store, routing, `run.py` console output. ~30 min.
3. Triage agent with tools, strict JSON parse, fallback. ~60 min.
4. Alerting via Resend. ~20 min.
5. Evals, commit `results.md`, fix anything the numbers expose. ~45 min.
6. README, screenshots, push, set repo description and topics. ~30 min.

**Definition of done:** public repo, evals committed with 100% fraud-typology recall and 0 false-positive declines, three screenshots in the README, fallback run proven, total agent cost under a dollar.

## 11. Outreach lines the repo earns

- "Built a miniature version of Oscilar's decision loop the afternoon I applied: rules plus a Claude triage agent plus human review routing, with committed evals: 5/5 seeded fraud typologies caught, zero false-positive declines, AML always routed to a human."
- Closing offer, per the standing rule: name a real decisioning or integration problem your team faces and I will come back with a scoped design or working prototype.
