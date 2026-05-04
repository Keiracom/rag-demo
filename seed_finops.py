"""Seed finops_demo schema with synthetic but realistic Australian financial data.

Run once: python3 seed_finops.py
"""
from __future__ import annotations

import os
import random
from datetime import date, timedelta

import psycopg

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
random.seed(42)

ACCOUNTS = [
    ("Wattle Family SMSF", "smsf", date(2018, 7, 1)),
    ("AustralianSuper Premium", "superannuation", date(2014, 3, 15)),
    ("Hostplus Choice Plus", "superannuation", date(2020, 11, 1)),
    ("CommSec Retail Trading", "retail", date(2021, 1, 10)),
    ("Macquarie Wholesale Equity", "wholesale", date(2017, 5, 22)),
]

# (ticker, asset_class, current_price_aud, vol_pct)
TICKERS = [
    ("VAS", "au_equity", 95.20, 0.012),    # Vanguard AU Shares ETF
    ("VGS", "intl_equity", 112.40, 0.010),  # Vanguard Intl Shares ETF
    ("VAP", "property", 88.10, 0.014),      # Vanguard AU Property ETF
    ("BHP", "au_equity", 42.15, 0.018),
    ("CBA", "au_equity", 145.80, 0.011),
    ("WBC", "au_equity", 32.40, 0.013),
    ("CSL", "au_equity", 268.50, 0.014),
    ("WES", "au_equity", 71.20, 0.012),
    ("FMG", "au_equity", 23.15, 0.022),
    ("RIO", "au_equity", 124.80, 0.016),
    ("AAPL.AX", "intl_equity", 285.40, 0.014),  # Stake AU listing
    ("MSFT.AX", "intl_equity", 558.20, 0.013),
    ("NVDA.AX", "intl_equity", 178.90, 0.025),
    ("VGB", "fixed_income", 47.30, 0.004),  # Aus Govt Bonds ETF
    ("IAF", "fixed_income", 99.85, 0.005),
    ("AAA", "cash", 50.05, 0.0001),         # Active Cash ETF
    ("BTC.AX", "crypto", 152000.00, 0.045),
    ("ETH.AX", "crypto", 6450.00, 0.052),
]

TODAY = date(2026, 5, 4)


def seed():
    conn = psycopg.connect(DB_URL, prepare_threshold=None)
    cur = conn.cursor()

    # Wipe old data (idempotent re-seed)
    cur.execute("TRUNCATE finops_demo.daily_performance, finops_demo.transactions, finops_demo.holdings, finops_demo.accounts RESTART IDENTITY CASCADE")

    # Accounts
    for name, atype, opened in ACCOUNTS:
        # Total account value seeded as ~holdings sum after generation; placeholder for now
        cur.execute(
            "INSERT INTO finops_demo.accounts (name, type, aud_value, opened_at) VALUES (%s,%s,%s,%s) RETURNING id",
            (name, atype, 0, opened),
        )

    cur.execute("SELECT id, opened_at FROM finops_demo.accounts ORDER BY id")
    accounts = cur.fetchall()

    # Holdings: 6-15 per account
    for acct_id, opened in accounts:
        n_holdings = random.randint(6, 15)
        chosen = random.sample(TICKERS, k=n_holdings)
        for ticker, asset_class, current_price, _vol in chosen:
            opened_h = opened + timedelta(days=random.randint(30, max(31, (TODAY - opened).days // 2)))
            avg_cost = round(current_price * random.uniform(0.65, 1.10), 4)
            units = round(random.uniform(50, 800), 4) if asset_class != "crypto" else round(random.uniform(0.05, 2.5), 4)
            cur.execute(
                "INSERT INTO finops_demo.holdings (account_id, ticker, asset_class, units, avg_cost_aud, current_price_aud, opened_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (acct_id, ticker, asset_class, units, avg_cost, current_price, opened_h),
            )

    # Transactions: 90 days, ~5/day across all accounts
    start_date = TODAY - timedelta(days=90)
    cur.execute("SELECT id, ticker, asset_class, current_price_aud FROM finops_demo.holdings ORDER BY account_id, id")
    all_holdings = cur.fetchall()
    holdings_by_acct: dict[int, list] = {}
    for h_id, ticker, ac, price in all_holdings:
        cur.execute("SELECT account_id FROM finops_demo.holdings WHERE id = %s", (h_id,))
        a = cur.fetchone()[0]
        holdings_by_acct.setdefault(a, []).append((ticker, ac, float(price)))

    for d in range(90):
        tx_date = start_date + timedelta(days=d)
        # weekdays only
        if tx_date.weekday() >= 5:
            continue
        n_tx = random.randint(0, 6)
        for _ in range(n_tx):
            acct_id = random.choice([a for a, _ in accounts])
            holdings = holdings_by_acct.get(acct_id, [])
            if not holdings:
                continue
            ticker, ac, price = random.choice(holdings)
            tx_type = random.choices(
                ["buy", "sell", "dividend", "contribution", "withdrawal", "fee"],
                weights=[35, 18, 12, 18, 8, 9],
            )[0]
            units = None
            tx_price = None
            if tx_type in ("buy", "sell"):
                units = round(random.uniform(10, 100), 4)
                tx_price = round(price * random.uniform(0.95, 1.05), 4)
                amount = round(units * tx_price * (1 if tx_type == "buy" else -1), 2)
            elif tx_type == "dividend":
                amount = round(random.uniform(50, 800), 2)
            elif tx_type == "contribution":
                amount = round(random.uniform(500, 5000), 2)
            elif tx_type == "withdrawal":
                amount = round(-random.uniform(200, 3000), 2)
            else:  # fee
                amount = round(-random.uniform(5, 80), 2)
                ticker = None
            cur.execute(
                "INSERT INTO finops_demo.transactions (account_id, tx_date, tx_type, ticker, units, price_aud, amount_aud) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (acct_id, tx_date, tx_type, ticker, units, tx_price, amount),
            )

    # Daily performance: 90 days, geometric brownian-ish walk
    for acct_id, opened in accounts:
        cur.execute("SELECT SUM(units * current_price_aud) FROM finops_demo.holdings WHERE account_id = %s", (acct_id,))
        base = float(cur.fetchone()[0] or 100000)
        cur.execute("UPDATE finops_demo.accounts SET aud_value = %s WHERE id = %s", (round(base, 2), acct_id))
        v = base * 0.92  # start 90 days ago lower
        for d in range(91):
            perf_date = start_date + timedelta(days=d)
            r = random.gauss(0.0006, 0.012)  # daily return ~0.06% mean, 1.2% sigma
            v = v * (1 + r)
            cur.execute(
                "INSERT INTO finops_demo.daily_performance (account_id, perf_date, total_value_aud, daily_return_pct) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (account_id, perf_date) DO NOTHING",
                (acct_id, perf_date, round(v, 2), round(r * 100, 4)),
            )

    conn.commit()
    cur.execute("SELECT COUNT(*) FROM finops_demo.accounts")
    a_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM finops_demo.holdings")
    h_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM finops_demo.transactions")
    t_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM finops_demo.daily_performance")
    p_count = cur.fetchone()[0]
    print(f"Seeded: {a_count} accounts, {h_count} holdings, {t_count} transactions, {p_count} performance rows")
    conn.close()


if __name__ == "__main__":
    seed()
