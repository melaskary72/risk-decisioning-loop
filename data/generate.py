"""Synthetic transaction generator with seeded ground truth.

No public dataset: we generate the stream ourselves so the labels are exact and
the evals in evals/run_evals.py are honest. `label` is written to the JSONL but
stripped before anything in engine/ sees a transaction.

Outputs (both gitignored, regenerate with `python data/generate.py`):
  data/transactions.jsonl   ~500 txns / 40 customers over 75 days
  data/events.jsonl         account events (device changes, password changes)

Amounts are USD-normalised; `currency` records the currency at the point of sale.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

SEED = 7
DATA_DIR = Path(__file__).resolve().parent
START = datetime(2026, 6, 1, 0, 0, 0)
SPAN_DAYS = 75

# (mcc, merchant pool) for ordinary spend
LEGIT_MCCS = [
    ("5411", ["SAFEWAY #221", "TRADER JOES 14", "WHOLE FOODS MKT", "KROGER 8812"]),
    ("5541", ["SHELL OIL 5567", "CHEVRON 0092", "BP CONNECT 41"]),
    ("5814", ["CHIPOTLE 1109", "SWEETGREEN SOMA", "STARBUCKS 00912"]),
    ("4899", ["NETFLIX.COM", "SPOTIFY USA", "COMCAST XFINITY"]),
    ("5912", ["CVS PHARMACY 7781", "WALGREENS 4410"]),
    ("5732", ["BEST BUY #442", "APPLE STORE R412"]),
    ("5942", ["BARNES & NOBLE 88"]),
    ("7011", ["MARRIOTT BONVOY", "HILTON GARDEN INN"]),
]
FOREIGN_MCCS = [
    ("5814", ["TRATTORIA DA ENZO", "BAR DEL FICO"]),
    ("7011", ["HOTEL SANTA CHIARA"]),
    ("5411", ["CARREFOUR EXPRESS RM"]),
    ("4111", ["ATAC ROMA METRO"]),
]

# Customers whose profiles are shaped for a specific seeded scenario.
CARD_TESTING = "CUST-011"
ATO = "CUST-017"
STRUCTURING = "CUST-023"
VELOCITY = "CUST-029"
FIRST_PARTY = "CUST-036"
LK_TRAVELLER = "CUST-004"
LK_TUITION = "CUST-008"
LK_DORMANT = "CUST-021"
LK_NEW_ACCOUNT = "CUST-033"
LK_NEW_DEVICE = ["CUST-002", "CUST-014", "CUST-026", "CUST-040"]

rnd = random.Random(SEED)


def build_customers() -> dict[str, dict]:
    customers: dict[str, dict] = {}
    for i in range(1, 41):
        cid = f"CUST-{i:03d}"
        home = rnd.choices(["US", "US", "US", "US", "US", "GB", "DE", "CA"], k=1)[0]
        base = rnd.choice([45, 60, 80, 110, 120, 150, 180, 220, 250, 300, 400])
        opened = START - timedelta(days=rnd.randint(180, 2200))
        customers[cid] = {
            "customer_id": cid,
            "home_country": home,
            "base_amount": base,
            "account_opened": opened,
            "devices": [f"DEV-{i:03d}A", f"DEV-{i:03d}B"],
        }

    # Scenario-shaped profiles.
    customers[CARD_TESTING].update(base_amount=80, home_country="US")
    customers[ATO].update(base_amount=150, home_country="US")
    customers[STRUCTURING].update(base_amount=300, home_country="US")
    customers[VELOCITY].update(base_amount=60, home_country="US")
    customers[LK_TRAVELLER].update(base_amount=120, home_country="US")
    customers[LK_TUITION].update(base_amount=400, home_country="US")
    customers[LK_DORMANT].update(base_amount=70, home_country="US")
    # Brand-new accounts: opened inside the stream window.
    customers[FIRST_PARTY].update(
        base_amount=90, home_country="US", account_opened=START + timedelta(days=70)
    )
    # One device, not a pool: a four-day-old account presenting three devices
    # reads as account resale, and the triage agent was right to say so.
    customers[LK_NEW_ACCOUNT].update(
        base_amount=220, home_country="US", account_opened=START + timedelta(days=66),
        devices=["DEV-033A"],
    )
    return customers


def txn(cust, ts, amount, merchant, mcc, *, country=None, device=None,
        is_new_device=False, currency="USD", label="legit", lookalike=None) -> dict:
    country = country or cust["home_country"]
    device = device or cust["devices"][0]
    age = (ts - cust["account_opened"]).days
    return {
        "customer_id": cust["customer_id"],
        "timestamp": ts.isoformat(timespec="seconds"),
        "amount": round(amount, 2),
        "currency": currency,
        "merchant": merchant,
        "mcc": mcc,
        "country": country,
        "device_id": device,
        "is_new_device": is_new_device,
        "account_age_days": max(age, 0),
        "label": label,
        "lookalike": lookalike,
    }


def legit_amount(cust) -> float:
    return cust["base_amount"] * rnd.uniform(0.5, 2.2)


def background(customers) -> list[dict]:
    """Ordinary spend: home country, known device, no velocity, gaps under 38 days."""
    out = []
    special_days = {
        VELOCITY: list(range(0, 21)),       # active, then dormant after day 20
        LK_DORMANT: list(range(0, 11)),     # goes quiet after day 10
        FIRST_PARTY: [],                    # account does not exist yet
        LK_NEW_ACCOUNT: [67, 68, 69],       # three small txns after opening
    }
    for cid, cust in customers.items():
        if cid in special_days:
            days = special_days[cid]
            n = len(days)
        else:
            n = rnd.randint(9, 15)
            for _ in range(200):
                days = sorted(rnd.sample(range(SPAN_DAYS), n))
                gaps = [b - a for a, b in zip(days, days[1:])]
                if not gaps or max(gaps) <= 38:
                    break
        for d in days:
            ts = START + timedelta(days=d, hours=rnd.randint(7, 22),
                                   minutes=rnd.randint(0, 59))
            mcc, merchants = rnd.choice(LEGIT_MCCS)
            if cid == LK_NEW_ACCOUNT:
                amount = {67: 200.0, 68: 240.0, 69: 280.0}[d]
            elif cid in (LK_DORMANT, VELOCITY, LK_TRAVELLER):
                amount = cust["base_amount"] * rnd.uniform(0.8, 1.2)
            else:
                amount = legit_amount(cust)
            out.append(txn(cust, ts, amount, rnd.choice(merchants), mcc,
                           device=rnd.choice(cust["devices"])))
    return out


# --------------------------------------------------------------------------
# Seeded fraud typologies
# --------------------------------------------------------------------------

def seed_card_testing(customers) -> list[dict]:
    """12 micro-authorisations at online merchants in 10 min, then a $900 attempt."""
    cust = customers[CARD_TESTING]
    t0 = START + timedelta(days=68, hours=10)
    online = ["CLOUDCART CHECKOUT", "FASTPAY GATEWAY", "SHOPFRONT API", "PAYLINK IO"]
    out = []
    for i in range(12):
        out.append(txn(cust, t0 + timedelta(seconds=50 * i), rnd.uniform(0.5, 3.0),
                       rnd.choice(online), "5967", label="card_testing"))
    out.append(txn(cust, t0 + timedelta(minutes=11), 900.00,
                   "ELECTRONICS DIRECT ONLINE", "5732", label="card_testing"))
    return out


def seed_account_takeover(customers) -> tuple[list[dict], list[dict]]:
    """New device + country jump + password change, then three draining transfers."""
    cust = customers[ATO]
    t0 = START + timedelta(days=71, hours=15)
    dev = "DEV-UNK-4471"
    events = [
        {"customer_id": ATO, "timestamp": (t0 - timedelta(minutes=25)).isoformat(timespec="seconds"),
         "event_type": "device_change", "detail": f"first-seen device {dev} from country RO"},
        {"customer_id": ATO, "timestamp": (t0 - timedelta(minutes=18)).isoformat(timespec="seconds"),
         "event_type": "password_change", "detail": "password reset via email link, new device, no MFA step-up"},
    ]
    amounts = [2300.00, 2600.00, 2400.00]
    out = [
        txn(cust, t0 + timedelta(minutes=20 + 15 * i), amt, "TRANSFER TO V PETRESCU",
            "4829", country="RO", device=dev, is_new_device=True, currency="EUR",
            label="account_takeover")
        for i, amt in enumerate(amounts)
    ]
    return out, events


def seed_structuring(customers) -> list[dict]:
    """Six cash-adjacent transfers just under $10k to one beneficiary over four days."""
    cust = customers[STRUCTURING]
    plan = [(65, 11), (65, 17), (66, 12), (67, 10), (68, 9), (68, 16)]
    amounts = [9100.0, 9450.0, 9800.0, 9200.0, 9600.0, 9350.0]
    return [
        txn(cust, START + timedelta(days=d, hours=h, minutes=rnd.randint(0, 59)),
            amt, "TRANSFER TO M ALVAREZ", "4829", label="structuring")
        for (d, h), amt in zip(plan, amounts)
    ]


def seed_velocity_spike(customers) -> list[dict]:
    """Dormant 64 days, then nine purchases inside twenty minutes."""
    cust = customers[VELOCITY]
    t0 = START + timedelta(days=72, hours=14)
    merchants = ["GAMESTOP 771", "TARGET 2210", "BEST BUY #118", "APPLE STORE R091",
                 "WALMART 4410", "HOME DEPOT 66", "COSTCO 331", "LOWES 812", "MACYS 09"]
    return [
        txn(cust, t0 + timedelta(minutes=2 * i), rnd.uniform(420, 650),
            merchants[i], "5732", label="velocity_spike")
        for i in range(9)
    ]


def seed_first_party_abuse(customers) -> list[dict]:
    """Account opened a day ago, max-limit purchase in a chargeback-prone category."""
    cust = customers[FIRST_PARTY]
    ts = START + timedelta(days=71, hours=2, minutes=40)
    return [txn(cust, ts, 2400.00, "PRIME GIFTCARD DIRECT", "5967",
                device="DEV-UNK-9902", is_new_device=True, label="first_party_abuse")]


# --------------------------------------------------------------------------
# Legitimate look-alikes: these are labelled legit and exist to make the
# false-positive rate measurable.
# --------------------------------------------------------------------------

def seed_lookalikes(customers) -> tuple[list[dict], list[dict]]:
    out, events = [], []

    # 1. Traveller abroad on a known device.
    cust = customers[LK_TRAVELLER]
    trip = [(60, 95.0), (61, 140.0), (62, 520.0), (63, 700.0), (63, 120.0), (64, 820.0)]
    for d, amt in trip:
        mcc, merchants = rnd.choice(FOREIGN_MCCS)
        out.append(txn(cust, START + timedelta(days=d, hours=rnd.randint(9, 21)),
                       amt, rnd.choice(merchants), mcc, country="IT", currency="EUR",
                       label="legit", lookalike="traveller_abroad"))

    # 2. Legitimate tuition payment in the AML amount band.
    cust = customers[LK_TUITION]
    out.append(txn(cust, START + timedelta(days=66, hours=13, minutes=5), 9500.00,
                   "STATE UNIVERSITY BURSAR", "8220",
                   label="legit", lookalike="tuition_payment"))

    # 3. Genuine device upgrades followed by a larger-than-usual purchase.
    for i, cid in enumerate(LK_NEW_DEVICE):
        cust = customers[cid]
        ts = START + timedelta(days=69 + i % 2, hours=18, minutes=10 * i)
        dev = f"{cust['devices'][0]}-NEW"
        events.append({
            "customer_id": cid,
            "timestamp": (ts - timedelta(hours=3)).isoformat(timespec="seconds"),
            "event_type": "device_change",
            "detail": f"device {dev} enrolled from home country with MFA step-up passed",
        })
        out.append(txn(cust, ts, cust["base_amount"] * 6.0, "BEST BUY #442", "5732",
                       device=dev, is_new_device=True,
                       label="legit", lookalike="device_upgrade"))

    # 4. Seasonal customer coming back after a quiet stretch.
    cust = customers[LK_DORMANT]
    out.append(txn(cust, START + timedelta(days=70, hours=11, minutes=20), 250.00,
                   "REI CO-OP 0084", "5941",
                   label="legit", lookalike="dormant_return"))

    # 5. New account, first big-ticket purchase on a properly enrolled device.
    #    Same reason codes as first-party abuse (DEV-001 + NEW-001); the thing
    #    that separates them is the enrolment evidence, which is the judgement
    #    call the agent is here to make.
    cust = customers[LK_NEW_ACCOUNT]
    ts = START + timedelta(days=70, hours=9, minutes=15)
    events.append({
        "customer_id": LK_NEW_ACCOUNT,
        "timestamp": (ts - timedelta(hours=6)).isoformat(timespec="seconds"),
        "event_type": "device_change",
        "detail": ("device DEV-033B enrolled from home country, MFA step-up passed, "
                   "replaces DEV-033A on the same account"),
    })
    out.append(txn(cust, ts, 650.00, "DELTA AIR LINES", "4511",
                   device="DEV-033B", is_new_device=True,
                   label="legit", lookalike="new_account_first_big_purchase"))
    return out, events


def main() -> None:
    customers = build_customers()
    txns = background(customers)
    events: list[dict] = []

    txns += seed_card_testing(customers)
    ato_txns, ato_events = seed_account_takeover(customers)
    txns += ato_txns
    events += ato_events
    txns += seed_structuring(customers)
    txns += seed_velocity_spike(customers)
    txns += seed_first_party_abuse(customers)
    lk_txns, lk_events = seed_lookalikes(customers)
    txns += lk_txns
    events += lk_events

    txns.sort(key=lambda t: t["timestamp"])
    for i, t in enumerate(txns, start=1):
        t["txn_id"] = f"TXN-{i:04d}"
    # Field order matching the spec, txn_id first.
    order = ["txn_id", "customer_id", "timestamp", "amount", "currency", "merchant",
             "mcc", "country", "device_id", "is_new_device", "account_age_days",
             "label", "lookalike"]
    txns = [{k: t[k] for k in order} for t in txns]
    events.sort(key=lambda e: e["timestamp"])
    for i, e in enumerate(events, start=1):
        e["event_id"] = f"EVT-{i:03d}"

    (DATA_DIR / "transactions.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in txns))
    (DATA_DIR / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))

    by_label: dict[str, int] = {}
    for t in txns:
        by_label[t["label"]] = by_label.get(t["label"], 0) + 1
    n_lookalike = sum(1 for t in txns if t["lookalike"])
    print(f"wrote {len(txns)} transactions, {len(events)} events, "
          f"{len({t['customer_id'] for t in txns})} customers")
    print(f"  window: {txns[0]['timestamp']} -> {txns[-1]['timestamp']}")
    for label, n in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<20} {n:>4}  ({n / len(txns):.1%})")
    print(f"  legit look-alikes    {n_lookalike:>4}")


if __name__ == "__main__":
    main()
