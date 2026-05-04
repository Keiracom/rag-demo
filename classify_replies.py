"""Classify replies marked status='replied_unknown' via Claude Haiku.

Reads each thread's latest non-us message, asks Claude to bucket as:
  replied_positive   — interested, scheduling, asking next steps
  replied_negative   — declining, wrong fit, unsubscribe
  followup_needed    — out-of-office, asking for info, ambiguous
Updates outreach.sends.status accordingly.

Run: python3 classify_replies.py
"""
from __future__ import annotations

import json
import os
import subprocess

import psycopg
from anthropic import Anthropic

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
GMAIL_MCP = "/home/elliotbot/clawd/mcp-servers/gmail-mcp/wrapper.sh"
CLAUDE = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

VALID_LABELS = {"replied_positive", "replied_negative", "followup_needed"}


def call_gmail_mcp(tool_name: str, arguments: dict):
    req_init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "classify", "version": "1"}}}
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": tool_name, "arguments": arguments}}
    p = subprocess.Popen([GMAIL_MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = (json.dumps(req_init) + "\n" + json.dumps(notif) + "\n" + json.dumps(req) + "\n").encode()
    out, _ = p.communicate(payload, timeout=60)
    for line in out.decode().splitlines():
        if '"id": 2' in line or '"id":2' in line:
            obj = json.loads(line)
            content = obj.get("result", {}).get("content", [])
            if content and content[0].get("text"):
                try:
                    return json.loads(content[0]["text"])
                except json.JSONDecodeError:
                    return content[0]["text"]
    return None


def classify(reply_body: str, original_subject: str) -> tuple[str, str]:
    """Returns (label, one-line rationale). Defaults to followup_needed on parse failure."""
    prompt = f"""You're triaging a reply to a cold outreach email. Original subject: "{original_subject}"

Reply body:
\"\"\"
{reply_body[:1500]}
\"\"\"

Classify this reply into EXACTLY ONE of these buckets:
- replied_positive: sender is interested, scheduling a call, asking specific next-step questions, asking for resume/portfolio
- replied_negative: sender is declining, telling us they're not interested, unsubscribing, complaining
- followup_needed: out-of-office auto-reply, ambiguous, or genuinely needs human follow-up to interpret

Respond with EXACTLY this format on two lines:
LABEL: <one of: replied_positive, replied_negative, followup_needed>
WHY: <one short sentence>"""
    msg = CLAUDE.messages.create(
        model="claude-haiku-4-5",
        max_tokens=120,
        messages=[{"role": "user", "content": prompt}],
    )
    text = (msg.content[0].text or "").strip()
    label = "followup_needed"
    why = "(parse failed)"
    for line in text.splitlines():
        if line.startswith("LABEL:"):
            candidate = line.split("LABEL:", 1)[1].strip().lower()
            if candidate in VALID_LABELS:
                label = candidate
        elif line.startswith("WHY:"):
            why = line.split("WHY:", 1)[1].strip()
    return label, why


def main():
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, gmail_thread_id, reply_message_id, subject, to_email "
            "FROM outreach.sends WHERE status = 'replied_unknown'"
        )
        rows = cur.fetchall()
        print(f"classifying {len(rows)} unknown replies")

        classified = 0
        for send_id, thread_id, reply_msg_id, subject, to_email in rows:
            # Pull the reply body via thread (more reliable than message_id alone)
            t = call_gmail_mcp("keiramail_read_thread", {"thread_id": thread_id})
            if not isinstance(t, dict):
                print(f"  {send_id}: read_thread failed")
                continue
            reply_body = ""
            for m in t.get("messages") or []:
                from_addr = (m.get("from") or "").lower()
                if "keiracom.com" not in from_addr:
                    reply_body = m.get("body") or ""
                    break
            if not reply_body:
                print(f"  {send_id}: no reply body found")
                continue
            label, why = classify(reply_body, subject or "")
            cur.execute(
                "UPDATE outreach.sends SET status = %s, notes = %s, updated_at = NOW() WHERE id = %s",
                (label, f"auto-classified: {why}", send_id),
            )
            classified += 1
            print(f"  {to_email[:30]:<30} → {label}  | {why[:80]}")
        conn.commit()
        print(f"\nDONE — {classified} replies classified")


if __name__ == "__main__":
    main()
