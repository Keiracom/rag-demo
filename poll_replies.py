"""Poll Gmail for replies to outreach.sends entries with status='sent'.
For each open thread, if a message exists in the thread NOT from us, mark as replied.

Run periodically (cron / Vercel cron / manual): python3 poll_replies.py
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

import psycopg

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
GMAIL_MCP = "/home/elliotbot/clawd/mcp-servers/gmail-mcp/wrapper.sh"
OUR_DOMAIN = "keiracom.com"


def call_gmail_mcp(tool_name: str, arguments: dict):
    req_init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "poll", "version": "1"}}}
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


def main():
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, gmail_thread_id, to_email FROM outreach.sends "
            "WHERE status = 'sent' AND gmail_thread_id IS NOT NULL"
        )
        open_sends = cur.fetchall()
        print(f"polling {len(open_sends)} open threads")

        new_replies = 0
        for send_id, thread_id, to_email in open_sends:
            res = call_gmail_mcp("keiramail_get_thread", {"thread_id": thread_id})
            if not res or not isinstance(res, dict):
                continue
            messages = res.get("messages") or []
            if len(messages) < 2:
                # Update last_polled_at, no reply
                cur.execute("UPDATE outreach.sends SET last_polled_at = NOW() WHERE id = %s", (send_id,))
                continue
            # Find message in thread NOT from us
            reply_msg = None
            for m in messages:
                from_addr = (m.get("from") or "").lower()
                if OUR_DOMAIN not in from_addr:
                    reply_msg = m
                    break
            if not reply_msg:
                cur.execute("UPDATE outreach.sends SET last_polled_at = NOW() WHERE id = %s", (send_id,))
                continue
            preview = (reply_msg.get("snippet") or "")[:400]
            cur.execute(
                """UPDATE outreach.sends
                   SET status = 'replied_unknown',
                       reply_message_id = %s,
                       reply_at = NOW(),
                       reply_preview = %s,
                       last_polled_at = NOW(),
                       updated_at = NOW()
                   WHERE id = %s""",
                (reply_msg.get("message_id"), preview, send_id),
            )
            new_replies += 1
            print(f"  REPLY from {to_email}: {preview[:80]}")
        conn.commit()
        print(f"\nDONE — {new_replies} new replies detected")


if __name__ == "__main__":
    main()
