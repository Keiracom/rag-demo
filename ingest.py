"""Fetch Anthropic public docs, chunk, embed, store in Supabase Postgres pgvector.

Usage: python3 ingest.py
"""
from __future__ import annotations

import os
import re
import sys
import time
from urllib.parse import urljoin

import httpx
import psycopg
from bs4 import BeautifulSoup
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

OPENAI = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

# Seed pages — Anthropic public docs landing pages we know exist
SEED_URLS = [
    "https://docs.claude.com/en/docs/intro",
    "https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview",
    "https://docs.claude.com/en/docs/build-with-claude/tool-use/overview",
    "https://docs.claude.com/en/docs/build-with-claude/extended-thinking",
    "https://docs.claude.com/en/docs/build-with-claude/prompt-caching",
    "https://docs.claude.com/en/docs/build-with-claude/citations",
    "https://docs.claude.com/en/docs/build-with-claude/files",
    "https://docs.claude.com/en/docs/build-with-claude/vision",
    "https://docs.claude.com/en/docs/build-with-claude/streaming",
    "https://docs.claude.com/en/docs/build-with-claude/structured-outputs",
    "https://docs.claude.com/en/docs/agents-and-tools/agent-sdk/overview",
    "https://docs.claude.com/en/docs/claude-code/overview",
    "https://docs.claude.com/en/docs/claude-code/quickstart",
    "https://docs.claude.com/en/docs/claude-code/subagents",
    "https://docs.claude.com/en/docs/claude-code/hooks",
    "https://docs.claude.com/en/docs/claude-code/skills",
    "https://docs.claude.com/en/docs/claude-code/mcp",
    "https://docs.claude.com/en/docs/claude-code/memory",
    "https://docs.claude.com/en/docs/agents-and-tools/computer-use",
    "https://docs.claude.com/en/api/messages",
    "https://docs.claude.com/en/api/models-list",
    "https://docs.claude.com/en/release-notes/claude-code",
]

CHUNK_TARGET_CHARS = 900   # ~225 tokens
CHUNK_HARD_MAX_CHARS = 4000  # safety cap well under OpenAI 8192-token limit


def fetch_clean(url: str) -> tuple[str, str] | None:
    """Fetch and clean a doc page. Returns (title, plain_text) or None on failure."""
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "keiracom-rag-demo/1.0"})
        if r.status_code != 200:
            print(f"  skip {url}: {r.status_code}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        title = (soup.title.string if soup.title else url).strip()
        main = soup.find("main") or soup.find("article") or soup.body
        text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
        text = re.sub(r"[ \t]+", " ", text)
        return title, text
    except Exception as e:
        print(f"  error {url}: {e}")
        return None


def chunk_text(text: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Chunk on paragraph→sentence→hard-split boundaries, packing up to target chars per chunk."""
    # Split on paragraph breaks first; fall back to sentence-end punctuation.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if len(paragraphs) <= 1:
        # No paragraph breaks — split on sentence boundaries
        paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        # If a single "paragraph" exceeds the hard max, hard-split it
        if len(p) > CHUNK_HARD_MAX_CHARS:
            for i in range(0, len(p), target):
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(p[i:i + target])
            continue
        if not current:
            current = p
        elif len(current) + len(p) + 2 <= target:
            current = current + "\n\n" + p
        else:
            chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) > 80 and len(c) <= CHUNK_HARD_MAX_CHARS]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings via OpenAI."""
    resp = OPENAI.embeddings.create(model="text-embedding-3-small", input=texts)
    return [d.embedding for d in resp.data]


def main():
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE rag_demo.chunks RESTART IDENTITY")
        conn.commit()
        total_chunks = 0
        for url in SEED_URLS:
            print(f"fetching {url}")
            res = fetch_clean(url)
            if not res:
                continue
            title, text = res
            chunks = chunk_text(text)
            if not chunks:
                print(f"  no chunks")
                continue
            # batch-embed (OpenAI supports up to 2048 inputs per call, but stay small)
            for i in range(0, len(chunks), 32):
                batch = chunks[i:i + 32]
                vectors = embed_batch(batch)
                rows = [
                    (url, title, i + j, batch[j], "[" + ",".join(f"{x:.6f}" for x in vectors[j]) + "]")
                    for j in range(len(batch))
                ]
                cur.executemany(
                    "INSERT INTO rag_demo.chunks (source_url, source_title, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    rows,
                )
                conn.commit()
                total_chunks += len(batch)
                print(f"  {len(batch)} chunks embedded (total {total_chunks})")
                time.sleep(0.2)
        print(f"\nDONE — {total_chunks} chunks across {len(SEED_URLS)} pages")


if __name__ == "__main__":
    main()
