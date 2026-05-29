# Security Review — v0

> Adversarial review of the v0 guardrails. Date: 2026-05-23. Reviewer: project author.
> Method: each security claim was attacked with a working exploit run against the
> real code, then cross-checked against the test suite. Findings that were genuine
> gaps are fixed in **v0.1** (this commit); model-dependent items are deferred to
> v1 with the reason recorded here and as `xfail(strict=True)` tests in
> `tests/test_adversarial.py`.

## Why this document exists

A passing unit test proves the *happy path* works. It says nothing about whether a
guardrail *fails safely when attacked*. This review exists to attack each of the
seven security claims in the README directly, document what held and what didn't,
and be honest about the difference between "fixed" and "deferred". Finding your own
holes is the skill; hiding them is the anti-pattern.

## Scorecard

| Claim | Verdict (v0) | After v0.1 | Notes |
|-------|--------------|------------|-------|
| 1. URL / SSRF validation | PARTIAL | **PASS** | Name-based + obfuscated-IP bypasses now resolved & re-checked. TOCTOU/pinned-fetch is v1 (no fetch path yet). |
| 2. File-size limit | PASS | PASS | Cap enforced before read; negative sizes rejected. |
| 3. Input sanitization / SQLi | PASS | PASS | Null bytes rejected, control chars stripped, all queries parameterized. Guard caveat noted below. |
| 4. Token budget | PASS | PASS | Coarse char heuristic; `tiktoken` upgrade is v1. |
| 5. Timeout controls | PASS | PASS | `run_with_timeout` wraps awaitables. |
| 6. Prompt-injection detection | PARTIAL (by design) | PARTIAL (by design) | Catches canonical phrasings; obfuscation/paraphrase bypass. Backstop = citation verification. Classifier is v1. |
| 7. Citation verification | PARTIAL | **PASS (false-positives)** / GAP (v1) | False rejections of valid variants fixed. Non-canonical hallucination still slips — closed in v1 via structured emission. |
| PII redaction (output) | PASS (structured) | PASS (structured) | All 7 structured types redacted, no over-redaction. Names/obfuscated forms need NER (v1). |
| API-key auth | PASS | PASS | Constant-time compare; fail-secure boot; all bad inputs denied. |
| Rate limiting | PASS | PASS + **edge guard** | Per-key limiter correct; added per-IP edge limiter ahead of auth (see finding 3). |

## Findings & exploits

### 1. SSRF — name-based bypass (was PARTIAL → fixed)
`validate_url` blocked literal internal IPs and known metadata hostnames, but anything
requiring name→IP resolution sailed through. Confirmed bypasses:
`http://localhost/`, `http://internal-db.mycorp.local/`, and the encoded-IP forms
`http://2130706433/` (32-bit int), `http://0x7f000001/` (hex), `http://0177.0.0.1/`
(octal), `http://127.0.0.1./` (trailing dot).

**Fix (v0.1):** `validate_url` now resolves the hostname (`_resolve_host`, injectable
for tests) and re-checks **every** resolved address against the blocklist; multi-answer
DNS with one internal record is blocked; unresolvable hosts **fail closed**.
**Still deferred (v1):** TOCTOU / DNS-rebinding — pin the resolved IP and fetch that
exact address. There is no fetch path in v0, so resolution-time checking is the correct
scope for now.

### 2. Citation verification — false positives AND false negatives (mixed → partly fixed)
The verifier only recognized one rigid surface form. It therefore (a) **rejected valid
citations** written differently ("GDPR Article 6", "GDPR Art.  6") and (b) **missed
hallucinations** phrased outside the pattern ("Article 250 of the GDPR").

**Fix (v0.1):** comparison now runs on a normalized form (`_normalize_citation`):
strip "Article"/"Art.", collapse whitespace, case-fold. Valid variants are accepted;
the canonical fake `GDPR Art. 99` is still caught.
**Still deferred (v1):** non-canonical hallucinations are not extracted at all. The fix
is to stop extracting citations from prose — have the LLM emit them in a structured
JSON field and verify that. This matters in v1 because an LLM will be generating the
text; in v0 the bodies are author-controlled CSV, so the live risk was the false
*positives*, which are now gone.

### 3. Unauthenticated flood → fsync amplification (GAP → fixed)
In the middleware, `verify_api_key()` ran before `limiter.check()`, so unauthenticated
requests returned 401 without ever being rate-limited — but each one still called
`gateway.deny()`, which `fsync`s an audit line to disk. An attacker with no key could
force one synchronous disk write per request, unbounded.

**Fix (v0.1):** added a per-IP **edge limiter** that runs *before* auth and writes no
audit row on the drop path, bounding the fsync’d deny path to N/window/IP. Behind a
reverse proxy (v2), trust a vetted `X-Forwarded-For` instead of the peer address.

### 4. Prompt-injection bypass (PARTIAL, by design — unchanged)
Caught: `ignore previous instructions`, `</system>`, `you are now unrestricted`.
Bypassed: leetspeak (`ign0re`), spacing, polite paraphrase, translation, data-embedded.
This is acceptable **by design** — the code documents the pattern detector as best-effort
with citation verification as the real backstop. The classifier upgrade is v1.

### 5. PII redaction (PASS for structured — unchanged)
All seven structured types redacted (email, CPR, SSN, card, IPv4, IBAN, phone) with the
CPR-before-phone ordering holding and no over-redaction of legitimate framework text.
Bare names and obfuscated forms leak; NER redaction is v1.

### Minor note — parameterized-query guard
`_assert_parameterized` only triggers when params are passed; SQL with interpolated
input and no params would slip past it. Not exploitable today (every tool uses `?`
binding), but flagged so a future change doesn't reintroduce the foot-gun.

## Reproducing this review

```bash
pytest tests/test_adversarial.py -q     # asserts every fixed behavior;
                                        # v1-deferred items are xfail(strict=True)
```

`xfail(strict=True)` means a deferred item that ever starts passing will be flagged by
the suite, prompting an update to this document — the limitations live in code, not just
in prose.

## v1 remediation summary
- **Structured citations:** LLM emits citations in a JSON field; verify that, drop regex-from-prose extraction.
- **SSRF TOCTOU:** resolve once, pin the IP, fetch that exact address.
- **NER PII:** add a model-based detector behind the existing regex first-pass.
- **Injection classifier:** layer a classifier over the pattern detector.
