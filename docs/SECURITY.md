# Security

This document states the security posture of the Privacy Compliance Toolkit
**as built**, and is explicit about what is deferred. The goal is honest scope,
not the appearance of completeness.

> An adversarial review of these controls -- the attacks run, what held, what was
> hardened in v0.1, and what remains deferred -- is in
> [SECURITY-REVIEW.md](SECURITY-REVIEW.md).

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
- **This is API-key authentication, not OAuth.** The key is a *static
  pre-shared secret*. The `Bearer` scheme is shared with OAuth, so the header
  looks identical, but there is no authorization server, no dynamic token
  issuance or exchange, no scopes, no expiry/refresh, and no per-user identity --
  every caller presenting the key has the same single trust level. This is a
  deliberate fit for a single-tenant, single-operator tool; OAuth would be
  over-engineering at this stage. OAuth 2.1 is the planned path once the server
  becomes multi-user (see *What is deferred*).

### Rate limiting
- In-memory, per-key sliding window (`PCT_MCP_RATE_LIMIT_REQUESTS` per
  `PCT_MCP_RATE_LIMIT_WINDOW_S`, default 60/60).
- Exceeding the budget returns HTTP 429. Audited like any other denial.
- A separate **per-IP edge limiter runs *before* authentication** (v0.1), so an
  unauthenticated flood is throttled before reaching the audited (fsync'd) denial
  path -- closing a disk-I/O amplification vector. It writes no audit row on the
  edge-drop path by design (that write is the amplification being prevented).

### Audit logging
- Every data access goes through the logging gateway, which writes an
  audit record (file, fsync'd, plus the `audit_log` table) **before** the access
  happens. Auth and rate-limit denials are audited too.
- The raw API key is never logged; only a short non-reversible fingerprint.

### Input / processing / output guardrails
- Input: SSRF-safe URL validation -- scheme allowlist, literal-IP blocklist,
  and (v0.1) **hostname resolution with a re-check of every resolved IP, failing
  closed on unresolvable hosts** (defeats `localhost`, internal DNS names, and
  integer/hex/octal IP encodings); file-size cap; text sanitization
  (null-byte/control-char rejection). All SQL is parameterized.
- Processing: token budget, request timeout, prompt-injection pattern detection.
- Output: PII redaction, confidence floor, citation-must-trace-back verification
  -- (v0.1) compared on a **normalized form** so valid citation variants
  (`GDPR Article 6` vs `GDPR Art. 6`) are not falsely rejected.

## What is deferred (and where)

| Area | Status | Plan |
|------|--------|------|
| **Encryption at rest** | Not implemented. SQLite + audit log are plaintext on disk. | v1: SQLCipher for the DB; OS-level disk encryption for logs. |
| **Encryption in transit (TLS)** | Not implemented. HTTP only. | v2: terminate TLS at uvicorn (`ssl_keyfile`/`ssl_certfile`) or a reverse proxy. |
| **Network allowlist (client IP)** | Not implemented. Server binds to `127.0.0.1`, so it is unreachable off-host -- the bind address is the current control. | v2: IP/CIDR allowlist enforced at the middleware edge, alongside TLS and a reverse proxy, once the server binds to a network interface. |
| **OAuth / multi-user auth** | Not implemented. One static API key, one trust level, no user identity. | v2: OAuth 2.1 authorization-server flow (per-user identity, scopes, token expiry/revocation) when the MCP server goes multi-user behind the FastAPI orchestrator. The MCP HTTP-transport spec leans toward OAuth for remote, multi-user servers. |
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
