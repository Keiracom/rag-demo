"""RAG search demo — hybrid retrieval (vector cosine + tsv keyword) + Claude answer synthesis with citations.

Run locally: uvicorn app:app --reload --port 8080
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from openai import OpenAI

# Allow .env loading in dev; on Railway env vars are set via dashboard
try:
    from dotenv import load_dotenv
    load_dotenv("/home/elliotbot/.config/agency-os/.env")
except Exception:
    pass

OPENAI = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Use anthropic SDK if available; else fall back to OpenAI for synthesis
try:
    from anthropic import Anthropic
    ANTHROPIC = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:
    ANTHROPIC = None


def search_hybrid(query: str, k: int = 8) -> list[dict]:
    """Hybrid retrieval: dense vector cosine + sparse tsv. Reciprocal rank fusion."""
    qvec = OPENAI.embeddings.create(model="text-embedding-3-small", input=[query]).data[0].embedding
    qvec_str = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
    sql = """
    WITH dense AS (
        SELECT id, source_url, source_title, chunk_index, content,
               1 - (embedding <=> %s::vector) AS score,
               ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rank
        FROM rag_demo.chunks
        ORDER BY embedding <=> %s::vector
        LIMIT 25
    ),
    sparse AS (
        SELECT id, source_url, source_title, chunk_index, content,
               ts_rank_cd(tsv, plainto_tsquery('english', %s)) AS score,
               ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %s)) DESC) AS rank
        FROM rag_demo.chunks
        WHERE tsv @@ plainto_tsquery('english', %s)
        ORDER BY score DESC
        LIMIT 25
    ),
    fused AS (
        SELECT COALESCE(d.id, s.id) AS id,
               COALESCE(d.source_url, s.source_url) AS source_url,
               COALESCE(d.source_title, s.source_title) AS source_title,
               COALESCE(d.chunk_index, s.chunk_index) AS chunk_index,
               COALESCE(d.content, s.content) AS content,
               COALESCE(1.0/(60 + d.rank), 0) + COALESCE(1.0/(60 + s.rank), 0) AS rrf_score
        FROM dense d
        FULL OUTER JOIN sparse s ON d.id = s.id
    )
    SELECT id, source_url, source_title, chunk_index, content, rrf_score
    FROM fused ORDER BY rrf_score DESC LIMIT %s;
    """
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute(sql, (qvec_str, qvec_str, qvec_str, query, query, query, k))
        rows = cur.fetchall()
    return [
        {"id": r[0], "url": r[1], "title": r[2], "chunk": r[3], "content": r[4], "score": float(r[5])}
        for r in rows
    ]


def synthesize_answer(query: str, hits: list[dict]) -> dict:
    """Use Claude (or GPT fallback) to synthesize a cited answer."""
    if not hits:
        return {"answer": "No relevant docs found in the corpus for that query.", "citations": []}

    context = "\n\n".join(
        f"[{i + 1}] {h['title']}\n{h['content']}"
        for i, h in enumerate(hits)
    )
    prompt = (
        f"You're answering a question from a small Anthropic-docs corpus. "
        f"Cite sources inline using bracketed numbers like [1], [3]. "
        f"If the docs don't cover it, say so plainly — don't invent.\n\n"
        f"DOCS:\n{context}\n\n"
        f"QUESTION: {query}\n\n"
        f"Answer concisely (3-6 sentences) with inline citations."
    )

    if ANTHROPIC:
        msg = ANTHROPIC.messages.create(
            model="claude-haiku-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
    else:
        # GPT fallback
        resp = OPENAI.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.choices[0].message.content

    cited_indices = sorted({int(m) for m in re.findall(r"\[(\d+)\]", text or "")})
    citations = [
        {"n": i, "title": hits[i - 1]["title"], "url": hits[i - 1]["url"], "preview": hits[i - 1]["content"][:200]}
        for i in cited_indices if 1 <= i <= len(hits)
    ]
    return {"answer": text or "", "citations": citations}


app = FastAPI(title="Keiracom RAG Demo")


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=2, max_length=500)):
    hits = search_hybrid(q, k=8)
    result = synthesize_answer(q, hits)
    return JSONResponse({"query": q, "answer": result["answer"], "citations": result["citations"], "hits": len(hits)})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/outreach")
def api_outreach():
    """Outreach tracker: list sends + status counts."""
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT status, COUNT(*) FROM outreach.sends GROUP BY status
        """)
        counts = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("""
            SELECT id, gmail_message_id, gmail_thread_id, to_email, to_company, subject,
                   source, status, sent_at, reply_at, reply_preview
            FROM outreach.sends
            ORDER BY sent_at DESC
            LIMIT 200
        """)
        sends = [
            {
                "id": r[0], "thread_id": r[2], "to_email": r[3], "company": r[4],
                "subject": r[5], "source": r[6], "status": r[7],
                "sent_at": r[8].isoformat() if r[8] else None,
                "reply_at": r[9].isoformat() if r[9] else None,
                "reply_preview": r[10],
            }
            for r in cur.fetchall()
        ]
    return JSONResponse({"counts": counts, "total": sum(counts.values()), "sends": sends})


@app.get("/outreach", response_class=HTMLResponse)
def outreach_page():
    return OUTREACH_HTML


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


OUTREACH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Outreach Tracker — Keiracom</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box }
  body { margin:0; background:#F7F3EE; color:#0F1419; font-family:'DM Sans', system-ui, sans-serif; line-height:1.4 }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 32px 24px }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing:-0.01em }
  .sub { color:#5A6470; font-size:13px; margin:0 0 24px }
  .stats { display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap }
  .stat { padding:14px 18px; background:white; border:1px solid #E8E2D8; border-radius:6px; min-width:100px }
  .stat .v { font-size:24px; font-weight:700; line-height:1 }
  .stat .l { font-family:'JetBrains Mono', monospace; font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:#5A6470; margin-top:6px }
  table { width:100%; border-collapse:collapse; background:white; border:1px solid #E8E2D8; border-radius:6px; overflow:hidden; font-size:13px }
  th { text-align:left; padding:10px 12px; background:#EFEAE0; font-family:'JetBrains Mono', monospace; font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:#5A6470; border-bottom:1px solid #E8E2D8 }
  td { padding:10px 12px; border-bottom:1px solid #F0EBE2; vertical-align:top }
  tr:last-child td { border-bottom:none }
  tr:hover td { background:#FBF8F2 }
  .pill { display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-family:'JetBrains Mono', monospace; font-weight:500 }
  .p-sent { background:#E8E2D8; color:#5A6470 }
  .p-replied_unknown { background:#FFF7E6; color:#8B6914 }
  .p-replied_positive { background:#D4F5E0; color:#1F7038 }
  .p-replied_negative { background:#F7D8D8; color:#992525 }
  .p-bounced { background:#F7D8D8; color:#992525 }
  .p-followup_needed { background:#F7E8D8; color:#8B5A30 }
  .src { font-family:'JetBrains Mono', monospace; font-size:11px; color:#5A6470 }
  .ts { font-family:'JetBrains Mono', monospace; font-size:11px; color:#5A6470 white-space:nowrap }
  a { color:#1E40AF; text-decoration:none }
  a:hover { text-decoration:underline }
  .reply-prev { color:#5A6470; font-size:12px; font-style:italic; margin-top:4px }
</style>
</head>
<body>
<div class="wrap">
  <h1>Outreach Tracker</h1>
  <p class="sub">Live status of all email outreach. Refresh for latest.</p>
  <div id="stats" class="stats"></div>
  <table>
    <thead><tr><th>Sent</th><th>To</th><th>Company</th><th>Subject</th><th>Source</th><th>Status</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>
<script>
async function load() {
  const r = await fetch('/api/outreach');
  const d = await r.json();
  const stats = document.getElementById('stats');
  stats.innerHTML = '<div class="stat"><div class="v">'+d.total+'</div><div class="l">Total Sent</div></div>';
  const order = ['sent','replied_unknown','replied_positive','replied_negative','bounced','followup_needed'];
  for (const k of order) {
    const v = d.counts[k] || 0;
    if (v === 0 && k !== 'sent') continue;
    stats.innerHTML += '<div class="stat"><div class="v">'+v+'</div><div class="l">'+k.replace(/_/g,' ')+'</div></div>';
  }
  const rows = document.getElementById('rows');
  rows.innerHTML = d.sends.map(s => {
    const ts = s.sent_at ? new Date(s.sent_at).toLocaleString('en-AU', {timeZone: 'Australia/Sydney', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}) : '';
    const replyBlock = s.reply_preview ? '<div class="reply-prev">↳ '+s.reply_preview.slice(0,140)+'…</div>' : '';
    const threadLink = s.thread_id ? '<a href="https://mail.google.com/mail/u/0/#all/'+s.thread_id+'" target="_blank">'+(s.subject||'(no subject)')+'</a>' : (s.subject||'(no subject)');
    return '<tr>'
      + '<td class="ts">'+ts+'</td>'
      + '<td>'+s.to_email+'</td>'
      + '<td>'+(s.company||'')+'</td>'
      + '<td>'+threadLink+replyBlock+'</td>'
      + '<td><span class="src">'+s.source+'</span></td>'
      + '<td><span class="pill p-'+s.status+'">'+s.status+'</span></td>'
      + '</tr>';
  }).join('');
}
load();
</script>
</body>
</html>"""

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Keiracom — RAG Demo (Anthropic Docs)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Live RAG search over Anthropic public docs. Hybrid retrieval + Claude answer synthesis with cited spans.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box }
  body { margin:0; background:#F7F3EE; color:#0F1419; font-family: 'DM Sans', system-ui, sans-serif; line-height:1.5 }
  .wrap { max-width: 760px; margin: 0 auto; padding: 48px 24px }
  header { margin-bottom: 32px }
  h1 { font-size: 28px; margin: 0 0 8px; letter-spacing:-0.01em }
  .sub { color:#5A6470; font-size:14px; margin:0 }
  .label { font-family:'JetBrains Mono', monospace; font-size:11px; color:#D4956A; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px }
  form { display:flex; gap:8px; margin-top:24px }
  input { flex:1; padding:14px 16px; border:1px solid #D4D8DC; border-radius:6px; font-size:16px; font-family:inherit; background:white; color:#0F1419 }
  input:focus { outline:2px solid #D4956A; outline-offset:1px }
  button { padding:14px 22px; background:#D4956A; color:white; border:none; border-radius:6px; font-weight:700; font-size:14px; cursor:pointer; font-family:inherit }
  button:disabled { opacity:0.5; cursor:wait }
  .answer { margin-top:32px; padding:20px; background:white; border-radius:6px; border:1px solid #E8E2D8; font-size:15px; white-space:pre-wrap; line-height:1.6 }
  .answer .cite { background:#F7E8D8; padding:1px 5px; border-radius:3px; font-family:'JetBrains Mono', monospace; font-size:12px; color:#8B5A30; text-decoration:none }
  .citations { margin-top:24px }
  .cit { padding:14px; background:white; border:1px solid #E8E2D8; border-radius:6px; margin-bottom:8px }
  .cit .n { font-family:'JetBrains Mono', monospace; font-size:11px; color:#D4956A; font-weight:700 }
  .cit .t { font-weight:500; margin:4px 0 }
  .cit a { color:#1E40AF; font-size:13px; text-decoration:none; font-family:'JetBrains Mono', monospace; word-break:break-all }
  .cit a:hover { text-decoration:underline }
  .cit .p { color:#5A6470; font-size:13px; margin-top:6px }
  .examples { margin-top:14px; font-size:13px; color:#5A6470 }
  .examples a { color:#8B5A30; text-decoration:none; margin-right:14px; cursor:pointer }
  .examples a:hover { text-decoration:underline }
  footer { margin-top:48px; padding-top:24px; border-top:1px solid #E8E2D8; font-size:13px; color:#5A6470 }
  footer a { color:#1E40AF; text-decoration:none }
  .err { color:#B91C1C; padding:14px; background:#FEF2F2; border:1px solid #FECACA; border-radius:6px; margin-top:16px }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="label">Keiracom · RAG Demo</div>
    <h1>Hybrid search over Anthropic docs</h1>
    <p class="sub">Live retrieval over 385 indexed chunks of <a href="https://docs.claude.com" style="color:#1E40AF">docs.claude.com</a>. Dense vector + keyword fusion, Claude answer with cited sources.</p>
  </header>
  <form id="f">
    <input id="q" placeholder='Ask anything: "how does prompt caching work?" / "what is a subagent?"' autofocus required>
    <button id="b" type="submit">Search</button>
  </form>
  <div class="examples">Try:
    <a onclick="ask('How does prompt caching work?')">How does prompt caching work?</a>
    <a onclick="ask('What are subagents in Claude Code?')">What are subagents?</a>
    <a onclick="ask('How do I use tool use with Claude?')">Tool use basics</a>
  </div>
  <div id="out"></div>
  <footer>
    Reference build by <a href="https://keiracom.com">Keiracom</a> — hybrid retrieval (pgvector cosine + tsvector keyword, RRF fusion) over Anthropic public docs, answer synthesis via Claude. <a href="https://github.com/Keiracom" style="color:#5A6470">github.com/Keiracom</a>
  </footer>
</div>
<script>
function ask(text){ document.getElementById('q').value = text; document.getElementById('f').dispatchEvent(new Event('submit')); }
const f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b'),o=document.getElementById('out');
f.addEventListener('submit', async e => {
  e.preventDefault();
  const query=q.value.trim();
  if(!query) return;
  b.disabled=true; b.textContent='Searching…'; o.innerHTML='';
  try {
    const r = await fetch('/api/search?q='+encodeURIComponent(query));
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    const ans = (d.answer||'').replace(/\\[(\\d+)\\]/g, '<a class="cite" href="#cit-$1">[$1]</a>');
    let html = '<div class="answer">'+ans+'</div>';
    if(d.citations && d.citations.length){
      html += '<div class="citations"><div class="label">Sources</div>';
      d.citations.forEach(c => {
        html += '<div class="cit" id="cit-'+c.n+'"><span class="n">['+c.n+']</span><div class="t">'+c.title+'</div><a href="'+c.url+'" target="_blank">'+c.url+'</a><div class="p">'+c.preview.replace(/</g,'&lt;')+'…</div></div>';
      });
      html += '</div>';
    }
    o.innerHTML = html;
  } catch(err) {
    o.innerHTML = '<div class="err">'+err.message+'</div>';
  } finally {
    b.disabled=false; b.textContent='Search';
  }
});
</script>
</body>
</html>"""
