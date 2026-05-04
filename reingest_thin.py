"""Re-ingest pages that produced thin content via direct httpx fetch.
Uses Camoufox to render the JS-loaded Anthropic docs pages.

Run after ingest.py has already populated the corpus.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

import psycopg
from camoufox.async_api import AsyncCamoufox
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv("/home/elliotbot/.config/agency-os/.env")

OPENAI = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

CHUNK_TARGET_CHARS = 900
CHUNK_HARD_MAX_CHARS = 4000

# Pages confirmed thin in first ingest — JS-rendered, need browser.
THIN_URLS = [
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
    "https://docs.claude.com/en/docs/agents-and-tools/computer-use",
    "https://docs.claude.com/en/api/messages",
    "https://docs.claude.com/en/api/models-list",
    "https://docs.claude.com/en/release-notes/claude-code",
]


def chunk_text(text: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks, current = [], ""
    for p in paragraphs:
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


def embed_batch(texts):
    return [d.embedding for d in OPENAI.embeddings.create(model="text-embedding-3-small", input=texts).data]


async def fetch_rendered(browser, url: str) -> tuple[str, str] | None:
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(1500)
        title = await page.title()
        text = await page.evaluate("""() => {
            const main = document.querySelector('main') || document.querySelector('article') || document.body;
            for (const sel of ['nav','header','footer','aside','script','style']) {
                main.querySelectorAll(sel).forEach(e => e.remove());
            }
            return main.innerText;
        }""")
        text = re.sub(r"\n{3,}", "\n\n", text or "")
        return title, text
    except Exception as e:
        print(f"  err {url}: {e}")
        return None
    finally:
        await page.close()


async def main():
    with psycopg.connect(DB_URL, prepare_threshold=None) as conn, conn.cursor() as cur:
        async with AsyncCamoufox(headless=True) as browser:
            total = 0
            for url in THIN_URLS:
                print(f"fetching {url}")
                res = await fetch_rendered(browser, url)
                if not res:
                    continue
                title, text = res
                chunks = chunk_text(text)
                if not chunks:
                    print(f"  still empty after render")
                    continue
                # Wipe old chunks for this URL, insert new
                cur.execute("DELETE FROM rag_demo.chunks WHERE source_url = %s", (url,))
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
                    total += len(batch)
                print(f"  {len(chunks)} chunks (total {total})")
                time.sleep(0.2)
        # Final count
        cur.execute("SELECT COUNT(*) FROM rag_demo.chunks")
        final = cur.fetchone()[0]
        print(f"\nDONE — {total} new chunks, corpus now {final} total")


if __name__ == "__main__":
    asyncio.run(main())
