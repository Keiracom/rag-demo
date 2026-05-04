"""Backfill outreach.sends from Dave's Gmail sent folder.

Pulls all messages sent in the last N hours, classifies by recipient/subject,
inserts/upserts into Supabase outreach.sends.

Run: python3 backfill_outreach.py [hours=24]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import psycopg

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
GMAIL_MCP = "/home/elliotbot/clawd/mcp-servers/gmail-mcp/wrapper.sh"


def call_gmail_mcp(tool_name: str, arguments: dict) -> dict | list:
    """One-shot stdio JSON-RPC call to keiramail mcp server. Returns parsed result content."""
    req_init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "outreach-backfill", "version": "1"}}}
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": tool_name, "arguments": arguments}}
    p = subprocess.Popen([GMAIL_MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = (json.dumps(req_init) + "\n" + json.dumps(notif) + "\n" + json.dumps(req) + "\n").encode()
    out, _ = p.communicate(payload, timeout=60)
    out_str = out.decode()
    # Extract the id=2 result line
    for line in out_str.splitlines():
        if '"id": 2' in line or '"id":2' in line:
            obj = json.loads(line)
            content = obj.get("result", {}).get("content", [])
            if content and content[0].get("text"):
                inner = content[0]["text"]
                try:
                    return json.loads(inner)
                except json.JSONDecodeError:
                    return inner
    return {}


def classify_source(to_email: str, subject: str) -> str:
    """Heuristic classification of a recipient into a source bucket."""
    to_low = (to_email or "").lower()
    subj_low = (subject or "").lower()
    # Dental BU outreach
    dental_domains = {"paddingtondentistry.com", "thepaddingtondentalsurgery.com.au", "nibdental.com.au",
                      "macquariedental.com.au", "bellevuehilldental.com.au"}
    if any(d in to_low for d in dental_domains):
        return "dental_bu"
    # Generic careers/apply/hiring inboxes
    if to_low.startswith(("apply@", "careers@", "hiring@", "jobs@", "humans@", "talent@", "career@", "work@", "humancapital@", "founders@", "eng.hiring@", "humans@", "hello@")):
        return "hn_who_is_hiring"
    # HN posters' personal +hn-tagged emails
    if "+hn@" in to_low:
        return "hn_who_is_hiring"
    # Specific company careers pages we probed
    if any(d in to_low for d in ["titan.ai", "manifestcyber.com", "haast.io"]):
        return "careers_page"
    return "other"


def extract_company(to_email: str) -> str:
    """Best-effort company name from email domain."""
    if not to_email or "@" not in to_email:
        return ""
    domain = to_email.split("@")[1]
    name = domain.split(".")[0]
    return name.replace("-", " ").title()


def parse_date(date_str: str) -> datetime:
    """Parse RFC822 Gmail date header into UTC datetime."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    print(f"Searching sent mail from last {hours} hours...")
    res = call_gmail_mcp("keiramail_search_messages", {"q": f"in:sent newer_than:{hours}h", "max_results": 20})
    if isinstance(res, dict) and "error" in res:
        print(f"  search err: {res['error']}")
        return
    # API returns either a dict with "results" key OR a bare list
    if isinstance(res, list):
        msgs = res
    elif isinstance(res, dict) and "results" in res:
        msgs = res["results"]
    else:
        print(f"  unexpected response shape: {str(res)[:200]}")
        return
    print(f"  found {len(msgs)} sent messages")

    inserted = 0
    skipped_existing = 0
    enriched = 0
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        for m in msgs:
            mid = m.get("message_id")
            tid = m.get("thread_id")
            # Search response lacks "to" — fetch full message to get To header
            to_email = ""
            full = call_gmail_mcp("keiramail_read_message", {"message_id": mid})
            if isinstance(full, dict):
                to_field = (full.get("to") or "").split(",")[0].strip()
                em = re.search(r"<([^>]+)>", to_field)
                to_email = em.group(1) if em else to_field
                enriched += 1
            subject = m.get("subject", "")
            sent_at = parse_date(m.get("date", ""))
            source = classify_source(to_email, subject)
            company = extract_company(to_email)
            cur.execute(
                """INSERT INTO outreach.sends (gmail_message_id, gmail_thread_id, to_email, to_company, subject, source, sent_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (gmail_message_id) DO UPDATE SET
                       to_email = EXCLUDED.to_email,
                       to_company = EXCLUDED.to_company,
                       source = EXCLUDED.source,
                       updated_at = NOW()
                   RETURNING (xmax = 0) AS was_insert""",
                (mid, tid, to_email, company, subject, source, sent_at),
            )
            row = cur.fetchone()
            if row and row[0]:
                inserted += 1
            else:
                skipped_existing += 1
        conn.commit()

    print(f"\nDONE — inserted {inserted}, updated {skipped_existing}, enriched {enriched}, total {len(msgs)}")


if __name__ == "__main__":
    main()
