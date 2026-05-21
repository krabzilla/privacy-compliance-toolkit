# Privacy Compliance Toolkit

> A security-by-design toolkit for privacy & compliance reasoning over GDPR, the Danish Data Protection Act, and NIST CSF 2.0. Built as a portfolio project for privacy/legal-tech work.

**Owner:** Kumari Rupali Bansal
**Status:** v0 (foundation) — see [Roadmap](#roadmap)
**License:** MIT

---

## What this is

Most "AI for compliance" demos wire an LLM directly to a vector store and call it a day. This project takes the opposite stance: **every data access goes through an audited gateway, every input is validated, every output is checked against the source framework**, and the LLM is treated as an untrusted component sitting inside a defense-in-depth perimeter.

The toolkit answers questions like:
- *"Does this processing activity satisfy GDPR Art. 6(1)(b)?"*
- *"Which NIST CSF 2.0 subcategories cover incident notification?"*
- *"Where does the Danish DPA tighten GDPR's defaults?"*

It refuses to answer (loudly) when its confidence is below threshold, when the citation doesn't trace back to a framework row, or when the input looks like prompt injection.

---

## Architecture

### Separation of duties

Four components, one job each. None of them talk to data directly — they all go through the **Logging Gateway**.

```
┌──────────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────────┐
│  Scraper /   │    │   RAG      │    │  MCP Server │    │ Orchestrator │
│  Framework   │    │  Engine    │    │  (FastMCP)  │    │  (FastAPI)   │
│  Loader      │    │ (v1)       │    │             │    │  (v2)        │
└──────┬───────┘    └─────┬──────┘    └──────┬──────┘    └──────┬───────┘
       │                  │                   │                  │
       │                  │                   │                  │
       ▼                  ▼                   ▼                  ▼
    ┌────────────────────────────────────────────────────────────────┐
    │              LOGGING GATEWAY (the only door to data)           │
    │  audit_log row written BEFORE access — fsync — fail loud       │
    └────────────────────────────────────────────────────────────────┘
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
           SQLite/PG         ChromaDB (v1)      Filesystem
```

### Defense in depth (5 layers)

| Layer | Purpose | v0 | v1 |
|-------|---------|-----|-----|
| **1. Input guardrails** | Reject malicious/oversized inputs at the door | SSRF block, size limit, sanitization | + prompt-injection pattern lib |
| **2. Processing guardrails** | Bound resource use, isolate the LLM | Timeout, token budget | + sandboxed prompt template |
| **3. Logging gateway** | Atomic audit of every data touch | ✅ | ✅ |
| **4. Output guardrails** | Verify before returning | Regex PII redaction, confidence floor | + citation-must-exist-in-framework check |
| **5. Trust pyramid** | Human sign-off on high-impact outputs | Confidence threshold gates | + reviewer queue |

### Security guardrails (the 7)

**Input**
1. **URL validation (SSRF)** — `guardrails/input.py::validate_url` rejects RFC1918, link-local, loopback, IPv6 ULA, and AWS/GCP/Azure metadata endpoints before any fetch.
2. **File size limit (DDoS)** — hard cap from `Config.MAX_FILE_SIZE_MB`, checked before read.
3. **Input sanitization (SQL injection, XSS)** — `sanitize_text` strips control chars, length-caps, and rejects null bytes; all DB access is parameterized.

**Processing**
4. **Token budget** — `guardrails/processing.py::enforce_token_budget` clips prompts to the per-call ceiling.
5. **Timeout controls** — `run_with_timeout` wraps every LLM and HTTP call.
6. **Prompt injection defence** — v0 ships a pattern-based detector (`detect_injection`); v1 adds a classifier.

**Output**
7. **Citation verification, PII redaction, confidence thresholds** — `guardrails/output.py` enforces all three before a response leaves the MCP server. Citations not found in the loaded framework cause the response to be rejected, not returned.

### Privacy by Design (Cavoukian, 7 principles)

| # | Principle | How it shows up here |
|---|-----------|----------------------|
| 1 | Proactive not reactive | Guardrails run before access, not after a complaint |
| 2 | Privacy as default | Disk persistence on; telemetry off; minimal logging fields |
| 3 | Embedded into design | Logging gateway isn't a wrapper — it's the only API to data |
| 4 | Positive-sum | Security ≠ usability tradeoff; failures are explicit, not silent |
| 5 | End-to-end security | TLS in transit (v2), at-rest encryption hook in `db.py` (v1) |
| 6 | Visibility and transparency | Every audit row is queryable; reports cite article-level sources |
| 7 | Respect for the user | PII is redacted in outputs even if it leaked through inputs |

### Detection modes — compliance vs risk

The toolkit distinguishes **compliance gaps** (a required control is missing) from **risk gaps** (a control exists but is weak in context). They use different prompts, different scoring, and different reviewer queues. See `docs/ARCHITECTURE.md` (added in v1).

---

## Frameworks

CSV schema, identical across frameworks:

| Column | Type | Description |
|--------|------|-------------|
| `Category` | string | Section/chapter of the framework |
| `Requirement` | string | Short title of the article/control |
| `Body` | string | Plain-language summary of what it requires |
| `Reference` | string | Canonical citation (e.g., `GDPR Art. 6`) |

Loaded files live in `data/frameworks/`:

- `gdpr.csv` — all 99 GDPR articles ✅ (v0)
- `danish_dpa.csv` — Danish supplements & derogations (v1)
- `nist_csf_2.csv` — NIST CSF 2.0 functions/categories/subcategories (v1)

> **Authoritative source disclaimer.** Bodies in `gdpr.csv` are author-drafted summaries for tooling; they are **not** the official text. Always verify against [EUR-Lex Regulation (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj) for legal use.

---

## Quick start

```bash
# 1. Clone & enter
git clone https://github.com/<your-handle>/privacy-compliance-toolkit.git
cd privacy-compliance-toolkit

# 2. Virtual env
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY

# 4. Initialise DB + load frameworks
python scripts/init_db.py
python scripts/load_frameworks.py

# 5. Generate an API key (once) and export it — the server won't start without one
python scripts/generate_api_key.py
export PCT_MCP_API_KEY="<the key it prints>"   # Windows PowerShell: $env:PCT_MCP_API_KEY="..."

# 6. Run the MCP server (HTTP, authenticated)
python -m src.mcp_server.server
# Listening on 127.0.0.1:8765 — every request needs Authorization: Bearer <key>
```

Calls must carry the key:

```bash
curl -H "Authorization: Bearer $PCT_MCP_API_KEY" http://127.0.0.1:8765/...
```

See [docs/SECURITY.md](docs/SECURITY.md) for the auth gate, rate limiting, key
rotation procedure, and what's deferred (encryption, monitoring, patching).

Tests:

```bash
pytest -q
```

---

## Repository layout

```
privacy-compliance-toolkit/
├── data/
│   ├── frameworks/
│   │   └── gdpr.csv               # 99 articles, Category/Requirement/Body/Reference
│   └── schema.sql                 # SQLite, PG-compatible
├── src/
│   ├── config.py                  # Secure defaults
│   ├── db.py                      # Connection helper, parameterized queries only
│   ├── logging_gateway.py         # The ONLY door to data
│   ├── guardrails/
│   │   ├── input.py               # URL/SSRF, size, sanitization
│   │   ├── processing.py          # Timeout, token budget, injection detect
│   │   └── output.py              # PII redaction, confidence, citation check
│   ├── frameworks/
│   │   └── loader.py              # CSV → DB ingest with schema validation
│   └── mcp_server/
│       └── server.py              # FastMCP tools
├── scripts/
│   ├── init_db.py
│   └── load_frameworks.py
├── tests/
│   ├── test_logging_gateway.py
│   ├── test_guardrails_input.py
│   ├── test_guardrails_output.py
│   └── test_db.py
├── docs/                           # ARCHITECTURE.md, SECURITY.md (v1)
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## Roadmap

### v0 — Foundation (this commit)
- Repository structure, README, license
- SQLite schema (PostgreSQL-ready)
- Logging gateway (atomic audit-before-access)
- Guardrail scaffolding — input (SSRF/size/sanitize), processing (timeout/token), output (regex PII/confidence)
- GDPR CSV with all 99 articles
- Framework loader with schema validation
- Basic FastMCP server: `list_frameworks`, `get_article`, `search_frameworks`
- MCP server hardening: API-key gate (constant-time, `secrets`-generated, env-only), per-key rate limiting, audited denials, `docs/SECURITY.md`
- Smoke tests

### v1 — Intelligence
- ChromaDB + HuggingFace `all-MiniLM-L12-v2` embeddings
- RAG engine with citation-must-trace-back enforcement
- Danish DPA + NIST CSF 2.0 frameworks loaded
- Full guardrail set — LLM-based injection detection, NER PII redaction, citation verification against loaded rows
- PDF report generation (`reportlab`) with compliance-gap and risk-gap modes
- Reviewer queue for sub-threshold outputs

### v2 — Surface
- React + Tailwind dashboard
- FastAPI orchestrator with role-based auth
- Docker Compose deployment (app + db + chroma)
- Live demo deployment

---

## Design references

- **Ann Cavoukian** — *Privacy by Design: The 7 Foundational Principles*
- **R. Jason Cronk** — *Strategic Privacy by Design* (2nd ed.) — particularly the logging-as-gateway pattern and the compliance-vs-risk gap distinction
- **IAPP CIPT** — Body of Knowledge
- **LinkedIn Learning** — *AI Security Tools and Automation*

---

## Contributing

This is a personal portfolio project; PRs aren't expected, but issues with framework data corrections are very welcome.

## Security

If you find a security issue, please open a private issue rather than a public one. The threat model assumes the MCP server may receive adversarial inputs; the guardrails are the contract.
