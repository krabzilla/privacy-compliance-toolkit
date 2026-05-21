# Security

This document states the security posture of the Privacy Compliance Toolkit
**as built**, and is explicit about what is deferred. The goal is honest scope,
not the appearance of completeness.

## Threat model (v0 / v1)

- **Deployment assumption:** single-tenant, single-operator. The MCP server is
  reached over HTTP on a host the operator controls.
- **Untrusted inputs:** tool arguments, URLs to fetch, and any text that reaches
  an LLM prompt are treated as adversarial. The guardrail layer is the contract.
- **Trusted components:** the operator, the host OS, and the configured LLM
  provider. These are out of scope for hardening here.
- **Out of scope:** multi-tenant isolation, untrusted co-located workloads, and
  defending the host itself.

## What is implemented

### Authentication (API-key gate)
- Every HTTP request to the MCP server must present a valid key in the
  `Authorization` header (`Bearer <key>`, or a raw key).
- The key is generated with Python's `secrets` module
  (`scripts/generate_api_key.py`, ~256 bits of entropy) and is **never** stored
  in source, notebooks, or a committed `.env`. It lives only in the environment
  (`PCT_MCP_API_KEY`).
- Comparison is constant-time (`secrets.compare_digest`) to avoid timing
  side-channels.
- The server **refuses to start** over HTTP if no key is configured
  (fail securely / secure default).

### Rate limiting
- In-memory, per-key sliding window (`PCT_MCP_RATE_LIMIT_REQUESTS` per
  `PCT_MCP_RATE_LIMIT_WINDOW_S`, default 60/60).
- Exceeding the budget returns HTTP 429. Audited like any other denial.

### Audit logging
- Every data access goes through the logging gateway, which writes an
  audit record (file, fsync'd, plus the `audit_log` table) **before** the access
  happens. Auth and rate-limit denials are audited too.
- The raw API key is never logged; only a short non-reversible fingerprint.

### Input / processing / output guardrails
- Input: SSRF-safe URL validation, file-size cap, text sanitization
  (null-byte/control-char rejection); all SQL is parameterized.
- Processing: token budget, request timeout, prompt-injection pattern detection.
- Output: PII redaction, confidence floor, citation-must-trace-back verification.

## What is deferred (and where)

| Area | Status | Plan |
|------|--------|------|
| **Encryption at rest** | Not implemented. SQLite + audit log are plaintext on disk. | v1: SQLCipher for the DB; OS-level disk encryption for logs. |
| **Encryption in transit (TLS)** | Not implemented. HTTP only. | v2: terminate TLS at uvicorn (`ssl_keyfile`/`ssl_certfile`) or a reverse proxy. |
| **Network allowlist (client IP)** | Not implemented. Server binds to `127.0.0.1`, so it is unreachable off-host -- the bind address is the current control. | v2: IP/CIDR allowlist enforced at the middleware edge, alongside TLS and a reverse proxy, once the server binds to a network interface. |
| **Key rotation** | Manual (see below). | v2: support multiple valid keys with overlap windows for zero-downtime rotation. |
| **Active monitoring / alerting** | Only audit logging exists (forensic, not real-time). | v2: `/metrics` endpoint (request count, p95 latency, guardrail-violation count) + alert rules. |
| **Dependency patching** | Manual; loose version pins. | v2: Dependabot config + `pip-audit` in CI; pin and review. |
| **Static analysis** | `ruff` lint configured. | v2: add `bandit` and CI gate. |
| **Secrets management** | Env vars only. | Out of scope for single-operator; document KMS/Vault path for production. |

## Key management procedure

**Generate** (once, on the operator's machine):

    python scripts/generate_api_key.py
    export PCT_MCP_API_KEY="<printed key>"

**Rotate** (on suspected compromise or on a schedule):

1. Generate a new key as above.
2. Update `PCT_MCP_API_KEY` in the server environment and restart.
3. Update every client that calls the server.
4. Treat the old key as burned.

> Single-key rotation implies brief downtime. Multi-key overlap is a v2 item.

**If a key is compromised:** rotate immediately, then review `audit_log` for
requests bearing the compromised key's fingerprint to scope the exposure.

## Reporting

Found an issue? Open a private security advisory on the repository rather than a
public issue.
