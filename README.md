# Keiracom RAG Demo — Hybrid Search Over Anthropic Docs

Live RAG demo — hybrid retrieval (pgvector cosine + tsvector keyword, fused via reciprocal rank) over the public Anthropic developer docs, answer synthesis with cited spans by Claude.

## Stack
- **Embeddings:** OpenAI `text-embedding-3-small`
- **DB:** Supabase Postgres + pgvector 0.8 (HNSW index) + pg_trgm + tsvector GIN
- **Backend:** FastAPI (Python 3.12)
- **Synthesis:** Claude Haiku 4.5 (with OpenAI gpt-4o-mini fallback)
- **Frontend:** single-file vanilla HTML/JS embedded in FastAPI route

## Run locally
```bash
pip install -r requirements.txt
python3 ingest.py        # populate corpus (Anthropic public docs)
uvicorn app:app --reload --port 8080
```

## Required env vars
- `DATABASE_URL` — Postgres URL with pgvector
- `OPENAI_API_KEY` — for embeddings + fallback synth
- `ANTHROPIC_API_KEY` — for Claude synthesis (optional; falls back to GPT)

## Architecture
Query → embed (OpenAI) → parallel dense (vector cosine, top 25) + sparse (tsvector, top 25) → reciprocal rank fusion (k=60) → top 8 → Claude synth with `[N]` inline citations → return JSON `{answer, citations, hits}`.

Built by [Keiracom](https://keiracom.com).
