# DocMind

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688.svg)
![Deps](https://img.shields.io/badge/deps-no%20PG%2FRedis%2FMongo-success.svg)

**English** · [简体中文](./README.md)

An AI Q&A assistant for open-source community / official documentation sites: give it a site URL + an LLM key, and you get "your own AI help center".

Use cases:
- Smart Q&A over a personal site / product docs / team wiki
- Fast retrieval across an internal knowledge base
- A self-hosted RAG demo / starter framework

> **Service positioning**: This service is designed to be exposed to the public internet as a "lightweight AI assistant". Security hardening is a major focus (see below), yet it depends on **no external middleware** (no Redis, no MongoDB). Single-process embedded storage — Docker isn't even required.

---

## ✨ Highlights

- 🌍 **Cross-lingual Q&A · internationalization out of the box**: even when the knowledge base is in Chinese, users can ask in **English / Japanese / Arabic** or any language — the system auto-detects the question's language and answers in **the same language**. Retrieval runs on BGE-M3 multilingual embeddings, so **one Chinese knowledge base serves users worldwide** — no document translation, no maintaining multiple knowledge bases. This is the most practical capability for a doc center facing overseas / multilingual users.
- ⚡ **Sub-second response · repeated questions don't burn money twice**: local vector retrieval + SSE streaming, with **time-to-first-token shown live** in the UI; an identical question (same context) is served straight from cache — **no repeated LLM call**, saving tokens and latency.
- 🛡️ **Public-internet-grade security**: 15 layers of defense (multi-dim rate limiting / daily token budget / prompt-injection interception / constant-time rejection …), detailed below.
- 🪶 **Extremely lightweight**: no Redis, no MongoDB, no Docker required; two SQLite files + one ChromaDB directory and it runs.

---

## 🛡️ Security Hardening (the focus)

When DocMind is exposed directly to the public internet it faces typical threats: endpoint abuse, burning LLM tokens (billing attacks), prompt injection, scraping scripts, proxy-pool IP rotation, connection-holding resource exhaustion, and data leakage. The sections below are organized as "attack technique → defense layer".

### 1. Multi-dimensional rate limiting (any dimension over threshold → 429)

Single-dimension limiting (e.g. IP only) is easy to bypass. This service stacks 4 dimensions, so an attacker must rotate IP + session + subnet + UA *simultaneously* to keep hammering — driving the cost up sharply:

| Dimension | Config | Default | Defends against |
|---|---|---|---|
| Per-IP RPM | `RATE_LIMIT_PER_MINUTE` | 12 | Single-point abuse |
| Per-session_id RPM | `RATE_LIMIT_PER_MINUTE_PER_SESSION` | 20 | Scripts that rotate IP but reuse session |
| Per-/24 subnet RPM (/64 for IPv6) | `RATE_LIMIT_PER_MINUTE_PER_SUBNET` | 60 | Proxy pools clustered in a few ASNs/subnets |
| Global instance RPM backstop | `GLOBAL_RATE_LIMIT_PER_MINUTE` | 300 | Distributed attacks |

All dimensions use a sliding-window algorithm (deque-of-timestamps) — no fixed-window boundary "burst at the edge" loophole.

### 2. Anti "billing attack": daily LLM token budget

The attacker's goal isn't to steal data — it's to burn your money: fire 10k requests to blow up the LLM bill. Defense:

- After each LLM call, add `usage.total_tokens` to the day's counter
- Before calling the LLM, check whether today's budget is already exhausted — **if over budget, refuse immediately without calling the LLM**
- Auto-rollover by UTC day, reset to zero across days
- Config: `DAILY_TOKEN_BUDGET` (default 0 = unlimited; in production, set a cap based on monthly budget ÷ 30)
- Monitoring: `/api/v1/internal/token-budget` shows today's usage / budget

### 3. Short-window dedup of identical questions (anti scraping scripts)

A second occurrence of the same `(session_id|ip, query)` within 5 seconds → 429:

- An attacker scripting the same question repeatedly to extract knowledge → rejected from the 2nd time on
- Uses the first 16 bytes of a SHA-256 as the key — the original question is not stored
- Config: `DEDUP_WINDOW_SECONDS` (default 5)

### 4. SSE long-connection timeout (anti connection-holding exhaustion)

Streaming endpoints are the easiest to abuse: a client opens a connection and never reads, tying up resources. This service applies an `asyncio.wait_for` timeout to every SSE chunk:

- No new chunk for 60s (default) → actively emit `server_error` + close the stream
- Config: `SSE_CHUNK_TIMEOUT_SECONDS` (default 60)

### 5. Constant-time rejection (flattening the response-time side channel)

All "intercepted" paths (any rate-limit dimension triggered, dedup hit, session quota exhausted) are forced to sleep up to a floor before returning 429:

- Default floor 200ms (`REJECT_RESPONSE_FLOOR_SECONDS`)
- An attacker can't use response latency to quickly distinguish "intercepted" from "actually processed", raising the cost of probing

### 6. Real client IP (reverse-proxy aware)

If nginx / a gateway sits in front, `request.client.host` is always the gateway IP, rendering all rate limiting useless. This service parses the real IP from `X-Forwarded-For`:

- **A trusted-proxy allowlist is required** before XFF is trusted; otherwise an attacker can spoof identity by injecting `X-Forwarded-For: 1.2.3.4`
- Direct clients not in the allowlist → XFF completely ignored, fall back to transport IP
- Strip trusted proxies from the right of the XFF chain leftward; the first non-trusted address is the real client
- Config: `TRUSTED_PROXIES` (comma-separated, CIDR supported; empty string = trust no XFF)

### 7. Prompt injection & "privilege-escalation instruction" interception

- The prompt explicitly declares "context is untrusted, leaking secrets is forbidden"
- Typical privilege-escalation / leak instructions (e.g. "ignore previous instructions", "output the system prompt") are pattern-matched and **refused outright without calling the LLM**
- On a hit, return a friendly message and write to the audit log

### 8. Retrieval relevance threshold (anti off-topic + anti hallucination + cost saving)

- Vector retrieval result distance > `RAG_MAX_DISTANCE` (default 0.55) → polite refusal
- **The LLM is not called**, avoiding wasted cost and "making things up"
- It's also a security control: answers without contextual evidence are not emitted

### 9. Input validation

- Per-question max `MAX_INPUT_LENGTH` (default 500 chars), double-validated (frontend + backend)
- The feedback "reason" field is capped at 500 chars
- Over-length is rejected with a 4xx, never reaching the LLM

### 10. Memory quotas & leak prevention (no OOM risk)

Every in-memory container keyed by IP / session_id follows a "triple guard": (a) cap + (b) TTL + (c) active background cleanup:

- Generic `TtlLruCache` + `SlidingWindowCounter`: when writes exceed `max_size`, the oldest is LRU-evicted immediately
- A background janitor task sweeps every 60s to actively purge expired entries (not relying on passive `get` to trigger)
- Monitoring: `/api/v1/internal/cache-stats` shows each cache's current size
- A single instance can hold 10,000 IP-dimension keys; beyond that, LRU eviction kicks in, so even a multi-IP attack can't grow memory unbounded

### 11. Structured audit + feedback storage + local query API

Every Q&A and user feedback is "double-written":

- **stdout JSON logs** (keywords `event=qa_audit` / `event=user_feedback`): dev can grep + jq in real time
- **SQLite tables** (standalone `audit.db` file, not in the repo): supports paginated queries + filtering + keyword search
- Auto-rolling deletion of records older than `AUDIT_RETENTION_DAYS` (default 30 days)
- Audit data **never enters the vector store** — you can't retrieve "what others asked" via RAG

### 12. Internal diagnostic API strictly localized

The `/api/v1/internal/*` routes (query feedback / audit / cache size / token budget):

- IP-allowlist middleware, default `127.0.0.1, ::1` only
- Config `INTERNAL_API_ALLOWED_IPS` (CIDR supported, e.g. `10.0.0.0/8`)
- **Deliberately does not trust X-Forwarded-For**: otherwise an attacker could bypass the allowlist by spoofing XFF
- Not exposed in the `/docs` OpenAPI

### 13. Global concurrency governance

- Global concurrent Q&A cap `MAX_CONCURRENT_QA` (default 20, includes SSE connections)
- Per-IP concurrent Q&A cap `MAX_CONCURRENT_QA_PER_IP` (default 3)
- Total sessions per IP `MAX_SESSIONS_PER_IP` (default 30)
- New sessions per IP per minute `MAX_NEW_SESSIONS_PER_MINUTE` (default 10)

### 14. Session isolation & privacy

- The frontend generates a `session_id` per browser tab, stored in `sessionStorage`
- The backend maintains conversation history per `session_id` (in-memory LRU + 30-minute TTL)
- **Strict isolation between sessions**: your session cannot access another's questions / history / cached answers
- When a session expires or is explicitly deleted, all answer caches tied to that session are cleared too

### 15. Log rotation (no disk-blowup risk)

- `.run/app.log` is self-managed by Python's `RotatingFileHandler`
- Default `100MB × 10` file rotation (`LOG_MAX_BYTES` + `LOG_BACKUP_COUNT`)
- The shell `nohup` redirects to a separate `app.stderr.log`, avoiding double-write clobbering with the Python log

### Security controls cheat sheet

| Attack technique | Defense layer | Config |
|---|---|---|
| Single-IP flooding | IP RPM limit | `RATE_LIMIT_PER_MINUTE` |
| Rotate IP, keep session | Session RPM | `RATE_LIMIT_PER_MINUTE_PER_SESSION` |
| Proxy pool / same ASN | /24 subnet RPM | `RATE_LIMIT_PER_MINUTE_PER_SUBNET` |
| Distributed RPM blowout | Global RPM backstop | `GLOBAL_RATE_LIMIT_PER_MINUTE` |
| Token-burning billing attack | Daily budget + refusal | `DAILY_TOKEN_BUDGET` |
| Scraping-script short replay | Identical-question dedup | `DEDUP_WINDOW_SECONDS` |
| Connection-holding SSE | Inter-chunk timeout | `SSE_CHUNK_TIMEOUT_SECONDS` |
| Response-time side-channel probing | Constant-time rejection floor | `REJECT_RESPONSE_FLOOR_SECONDS` |
| IP distortion behind proxy | XFF + trusted proxies | `TRUSTED_PROXIES` |
| Prompt injection / escalation | Pattern interception + prompt declaration | (built-in) |
| Off-topic / out-of-scope questions | Retrieval distance threshold | `RAG_MAX_DISTANCE` |
| Memory blown by many IPs | LRU + TTL + janitor | (built-in) |
| Audit/feedback data queried back | Audit kept out of vector store | (architectural isolation) |
| Logs blowing up the disk | RotatingFileHandler | `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` |

---

## Core capabilities

- Site crawling & cleaning: crawl pages from a start URL, extract the main content and clean the text
- Auto chunking & vectorization: write to ChromaDB, supports semantic retrieval
- Retrieval-augmented Q&A: generate answers via an OpenAI-compatible LLM grounded in context
- Conversational memory: follow-up questions under the same `session_id` keep continuous context
- Source attribution: answers return reference URLs
- Multilingual adaptation (internationalization): auto-detects the question's language and replies in the same language — a Chinese knowledge base can still be queried in English / Japanese / Arabic and more; also supports asking in Chinese to retrieve English docs (BGE-M3 multilingual embeddings)
- SSE streaming output: typewriter effect + suggested follow-ups
- Feedback mechanism: each answer supports 👍/👎, 👎 can include a reason (up to 500 chars) for continuous improvement

---

## Tech stack

- **Backend**: FastAPI + Uvicorn (async)
- **Vector store**: ChromaDB (embedded, persistent)
- **Embedding**: BAAI/bge-m3 local model
- **LLM**: OpenAI-compatible API (DeepSeek by default)
- **Storage**: two SQLite files (`audit.db` + `crawl_meta.db`), **no Redis, no MongoDB**
- **Logging**: structlog JSON + RotatingFileHandler
- **Frontend**: vanilla HTML/JS + TailwindCSS

Lean overall: **no Docker dependency, no external database dependency**. `run.sh` brings up uvicorn directly.

---

## Quick start

### Requirements

- Python 3.9+ (3.11 for development)
- RAM ≥ 4 GB (BGE-M3 resident is ~2.3 GB)
- Disk ≥ 5 GB (model ~2.1 GB + knowledge base + logs)
- An OpenAI-compatible LLM API key (DeepSeek by default; any compatible gateway works)

> The entire flow **needs no Docker and no external database**. Below are the complete steps to run the service locally on bare metal.

### 1) Clone + install dependencies

```bash
git clone https://github.com/lukyFun/search-ai.git docmind && cd docmind
pip install -r requirements.txt
```

> The model files (~2.1 GB) are not in the repo (gitignored); download them in the next step.

### 2) Download the embedding model (BAAI/bge-m3)

Pick one:

```bash
# Option A: overseas / can reach HuggingFace
python3 scripts/download_model.py

# Option B: recommended in China, via ModelScope (Aliyun mirror, fast)
pip install modelscope
python3 scripts/download_model_cn.py
```

Both scripts download the model to `models/bge-m3/`.

> You can also skip this step: if the model directory doesn't exist, the service auto-pulls `BAAI/bge-m3` from HuggingFace on first start.
> But auto-pull is slow or may fail on networks in China, so **pre-downloading via Option B is strongly recommended**.

### 3) Configure `.env`

```bash
cp .env.example .env
# then edit .env, at minimum set LLM_API_KEY
```

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_NAME=deepseek-chat

# To switch doc sites, change only these lines (persona / knowledge scope / sample questions / crawl entry)
ASSISTANT_NAME=云文档智能问答助手
KNOWLEDGE_SCOPE=人脸识别产品文档
EXAMPLE_QUESTIONS=人脸识别是什么？,人脸识别的计费方式是怎样的？
TARGET_URL=https://cloud.tencent.com/document/product/867
```

> The repo ships with a demo target of the **Tencent Cloud Face Recognition product docs** (a public-docs example).
> To switch to your own site: point `TARGET_URL` at the target doc entry and change the three branding items `ASSISTANT_NAME` / `KNOWLEDGE_SCOPE` / `EXAMPLE_QUESTIONS` — **no code changes needed**.

### 4) Start

```bash
bash run.sh          # dev mode (--reload), background
bash run.sh --prod   # prod mode
bash run.sh --fg     # foreground
APP_PORT=8101 bash run.sh   # change port (default 8100)
```

Stop: `bash stop.sh`. The first start loads the BGE-M3 model, taking ~5–10 seconds.

### 5) Build the knowledge base

Once the service is up, ingest some documents (verify the pipeline with a small page count first):

**Via CLI**:

```bash
# Dry-run preview of the crawl result first, without writing to the store
python3 scripts/ingest_cli.py --mode preview --url "https://cloud.tencent.com/document/product/867" --limit 5
# Once it looks good, write for real
python3 scripts/ingest_cli.py --mode ingest --url "https://cloud.tencent.com/document/product/867" --limit 50
```

**Via API**:

```bash
curl -X POST "http://localhost:8100/api/v1/ingest" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://cloud.tencent.com/document/product/867", "max_pages": 50}'
```

### 6) Access

- Web UI: `http://localhost:8100`
- OpenAPI: `http://localhost:8100/docs`
- Health check: `http://localhost:8100/health`

---

## Adapting to your own doc site

The repo ships with a Tencent Cloud Face Recognition product docs demo. Switching to another site usually requires **config changes only**:

1. In `.env`, change the three branding items + crawl entry: `ASSISTANT_NAME` / `KNOWLEDGE_SCOPE` / `EXAMPLE_QUESTIONS` / `TARGET_URL`
2. Frontend copy is changed in one place — the `UI_CONFIG` block at the top of `app/static/index.html` (title / welcome text / preset questions / input placeholder)
3. Re-ingest the knowledge base

Two key crawler behaviors:

- **Crawl scope is bounded by the path prefix**: using the `TARGET_URL` path as a prefix, it only crawls pages under that directory.
  Setting the start URL to `cloud.tencent.com/document/product/867` crawls only the Face Recognition docs, without following links into the whole site.
- **Automatic main-content container detection**: it tries in order `<article>` → `<main>` → `.J-mainDetail` / `.J-markdown-box` (Tencent Cloud docs) / `.markdown-body` (common markdown themes), falling back to the whole `<body>` only if none match.

> ⚠️ Prerequisite: the target site's main content must be in **static HTML** (server-side rendered). Tencent Cloud docs, Docusaurus, etc. all qualify.
> If it's a pure SPA whose content is rendered entirely by async JS, httpx fetches an empty shell — you'd need to integrate a headless browser or the site's content API, which this project doesn't currently cover.
> If the main-content container is a custom structure none of the above selectors match, add a class to the container list in `app/services/crawler.py`.

---

## Environment variables

### Basics

| Variable | Default | Description |
|---|---|---|
| `PROJECT_NAME` | `DocMind` | Project name (OpenAPI title) |
| `VERSION` | `1.0.0` | Version |
| `API_V1_STR` | `/api/v1` | API prefix |
| `DEBUG` | `False` | Debug switch |

### Branding / knowledge scope (change only these three to switch sites)

| Variable | Default | Description |
|---|---|---|
| `ASSISTANT_NAME` | `云文档智能问答助手` | Assistant persona in the system prompt and frontend title |
| `KNOWLEDGE_SCOPE` | `人脸识别产品文档` | The X in "I only answer questions within X" in the refusal message |
| `EXAMPLE_QUESTIONS` | `人脸识别是什么？,…` | Sample questions shown to the user on refusal (comma-separated) |

### LLM

| Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | `dummy-key` | LLM API key (**must change**) |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible gateway |
| `LLM_MODEL_NAME` | `deepseek-chat` | Model name |

### Vector store / model

| Variable | Default | Description |
|---|---|---|
| `CHROMA_PERSIST_DIRECTORY` | `data/chromadb` | Chroma persistence directory |
| `MODEL_PATH` | `models/bge-m3` | Local embedding model |
| `RAG_MAX_DISTANCE` | `0.55` | Retrieval relevance threshold |
| `MAX_CONTEXT_LENGTH` | `4000` | Max RAG context characters |

### Crawler

| Variable | Default | Description |
|---|---|---|
| `TARGET_URL` | `https://cloud.tencent.com/document/product/867` | Default crawl entry; scope bounded by its **path prefix** |

### Data storage

| Variable | Default | Description |
|---|---|---|
| `AUDIT_DB_PATH` | `data/audit.db` | Audit / feedback SQLite (not in repo) |
| `CRAWL_DB_PATH` | `data/crawl_meta.db` | Crawler metadata SQLite (may be committed) |
| `AUDIT_RETENTION_DAYS` | `30` | Audit data retention days |
| `AUDIT_JANITOR_INTERVAL_SECONDS` | `3600` | Audit cleanup interval |

### Logging

| Variable | Default | Description |
|---|---|---|
| `LOG_FILE` | `.run/app.log` | Main log path |
| `LOG_MAX_BYTES` | `100MB` | Per-file size cap |
| `LOG_BACKUP_COUNT` | `10` | Rotation backup count |

### Security / rate limiting / reverse proxy

| Variable | Default | Description |
|---|---|---|
| `MAX_INPUT_LENGTH` | `500` | Max question characters |
| `RATE_LIMIT_PER_MINUTE` | `12` | Per-IP RPM |
| `RATE_LIMIT_PER_MINUTE_PER_SESSION` | `20` | Per-session RPM |
| `RATE_LIMIT_PER_MINUTE_PER_SUBNET` | `60` | /24 subnet RPM |
| `GLOBAL_RATE_LIMIT_PER_MINUTE` | `300` | Global instance RPM backstop |
| `MAX_CONCURRENT_QA` | `20` | Global concurrent Q&A |
| `MAX_CONCURRENT_QA_PER_IP` | `3` | Per-IP concurrent Q&A |
| `MAX_SESSIONS_PER_IP` | `30` | Total sessions per IP |
| `MAX_NEW_SESSIONS_PER_MINUTE` | `10` | New sessions per IP per minute |
| `DAILY_TOKEN_BUDGET` | `0` | Daily LLM token budget, 0 = unlimited |
| `DEDUP_WINDOW_SECONDS` | `5` | Identical-question dedup window |
| `SSE_CHUNK_TIMEOUT_SECONDS` | `60` | SSE chunk timeout |
| `REJECT_RESPONSE_FLOOR_SECONDS` | `0.2` | Constant-time rejection floor |
| `INTERNAL_API_ALLOWED_IPS` | `127.0.0.1,::1` | Internal API allowlist (CIDR) |
| `TRUSTED_PROXIES` | (empty) | Trusted reverse-proxy allowlist; empty = trust no XFF |

---

## Ops query endpoints (local / intranet only)

All `/api/v1/internal/*` routes are protected by the IP-allowlist middleware and **do not accept X-Forwarded-For** (anti-spoofing).

```bash
# Latest 50 feedback entries (default: newest first)
curl "http://localhost:8100/api/v1/internal/feedback?limit=50"

# Only downvotes + keyword
curl "http://localhost:8100/api/v1/internal/feedback?vote=down&keyword=inaccurate"

# Query audit logs (QA records)
curl "http://localhost:8100/api/v1/internal/audit?session_id=xxx"
curl "http://localhost:8100/api/v1/internal/audit?client_ip=1.2.3.4&since_ms=1700000000000"

# Current size of each in-memory cache (to check "is memory growing")
curl "http://localhost:8100/api/v1/internal/cache-stats"

# Today's used / budget tokens
curl "http://localhost:8100/api/v1/internal/token-budget"
```

Devs can also `grep` keywords in the structured logs directly (no API needed):

```bash
# All QA audits
grep '"qa_audit"' .run/app.log | jq

# All feedback from a given IP
grep '"user_feedback"' .run/app.log | jq 'select(.client_ip=="1.2.3.4")'

# Rate-limit trigger records
grep -E '"rate_limit_(ip|subnet|session|global)"' .run/app.log | jq
```

---

## Feedback mechanism (👍 / 👎)

- The Web UI offers 👍 / 👎 below each AI answer
- 👎 can include a "reason for dissatisfaction" (up to 500 chars)
- Backend: `POST /api/v1/feedback` double-writes:
  - structlog log `event=user_feedback`
  - SQLite `user_feedback` table (queryable via `/internal/feedback`)

---

## Multimodal (not implemented)

The current version supports text-only RAG. To extend to PDF / images / audio-video:

- PDF / Office: parse text → chunk → vectorize
- Images: OCR text extraction / image embedding models
- Audio-video: ASR transcription (can be chunked along the timeline)

The crawl / chunk / index pipeline is reusable; the extension point is the "content extraction" layer.

---

## Project structure

```text
app/
├── api/v1/
│   ├── chat.py        # Q&A endpoint (incl. SSE)
│   ├── ingest.py      # Knowledge base building
│   └── internal.py    # Ops query endpoints (local allowlist)
├── core/
│   ├── config.py        # Config (pydantic-settings)
│   ├── cache.py         # TtlLruCache + SlidingWindowCounter + janitor
│   ├── security.py      # Multi-dim rate limiting + quota + dedup + constant-time rejection
│   ├── net.py           # IP allowlist / XFF parsing
│   ├── sqlite_store.py  # audit.db (qa_audit + user_feedback)
│   ├── crawl_store.py   # crawl_meta.db (crawler documents)
│   ├── token_budget.py  # Daily LLM token budget
│   ├── logger.py        # structlog + RotatingFileHandler
│   └── exceptions.py    # Global exceptions
├── services/
│   ├── crawler.py       # Crawler
│   ├── llm_service.py   # RAG orchestration + LLM calls
│   └── vector_service.py
├── static/              # Web UI
└── main.py

data/
├── audit.db             # Generated at runtime (not in repo)
├── crawl_meta.db        # May be committed (ship the knowledge base with the image)
└── chromadb/            # ChromaDB persistence

scripts/
└── ingest_cli.py        # Crawl CLI
```

---

## FAQ

### 1) Chat returns 401 / auth failure

Check `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` in `.env`.

### 2) View logs

```bash
tail -f .run/app.log         # Structured JSON logs
tail -f .run/app.stderr.log  # Startup-phase stderr fallback
```

Every log line carries a `request_id`; use jq to trace the full chain.

### 3) How is conversational memory implemented?

- Frontend: each browser tab stores a UUID in `sessionStorage` as the `session_id`
- Backend: in-memory LRU + 30-minute TTL; strict isolation between sessions, no access to others' records
- On session expiry / explicit delete, the answer cache is cleared too

### 4) Can it persist without Docker / an external database?

Yes. Two SQLite files + one ChromaDB directory, all under `data/`.
The image build can bundle `crawl_meta.db` and `chromadb/`, so a git clone + `bash run.sh` brings up a service with the knowledge base preloaded.

---

## License

[MIT](LICENSE) © 2026 lukyFun
