#!/usr/bin/env python3
"""
analyze_notice.py -- grade a privacy NOTICE against the GDPR notice checklist.

Unlike analyze_policy.py (which grades against the full 99-article framework via
the MCP server), this runs the v1.5 CHECKLIST analyzer IN-PROCESS -- no server,
no API key. It scores the policy only against the disclosures a public notice
must actually make (GDPR Arts. 12-14 + the Danish CPR overlay), filtered by the
org profile you declare.

RIGOROUS BY DEFAULT: every applicable requirement is sent to the LLM with a
strict grading rubric (vague/boilerplate language scores "partial", not
"covered"); there is no semantic auto-pass. This is slower (one LLM call per
applicable requirement) but trustworthy. Use --fast for the hybrid path that
auto-passes high-similarity items, or --no-llm for a semantic-only sweep.

Prereqs:
    * pip install -r requirements.txt   (sentence-transformers, pyyaml, httpx)
    * for LLM verification: Ollama running with the model in PCT_LLM_MODEL
      (default mistral:7b-instruct).

Usage:
    python scripts/analyze_notice.py my_policy.txt \
        --profile data_collected_directly,legal_basis_includes_consent,transfers_outside_eea,cpr_processed
    python scripts/analyze_notice.py my_policy.txt --fast      # hybrid, faster
    python scripts/analyze_notice.py my_policy.txt --no-llm    # semantic only
    python scripts/analyze_notice.py my_policy.txt --json report.json

Declarable profile facts (see data/checklists/gdpr_notice_requirements.yaml):
    data_collected_directly, data_collected_indirectly,
    legal_basis_includes_legitimate_interest, legal_basis_includes_consent,
    transfers_outside_eea, automated_decision_making_present,
    special_category_data, controller_outside_eu, dpo_appointed, cpr_processed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running as `python scripts/analyze_notice.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402

# How many policy passages each requirement's verifier sees in rigorous mode.
# Higher than the analyzer default so the strict grader has enough context to
# avoid false "gap" calls from a disclosure sitting in a chunk it never saw.
RIGOROUS_TOP_CHUNKS = 6


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Grade a privacy notice against the GDPR notice checklist.")
    p.add_argument("policy_file", help="Path to the policy/notice text file.")
    p.add_argument("--profile", default="",
                   help="Comma-separated org-profile facts (default: none -> only 'always' requirements graded).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--fast", action="store_true",
                      help="Hybrid path: auto-pass high-similarity items, only LLM-verify borderline ones.")
    mode.add_argument("--no-llm", action="store_true",
                      help="Semantic coverage only; no LLM calls (fastest, least rigorous).")
    p.add_argument("--json", dest="json_out", default=None,
                   help="Write the full report JSON here (default: <policy>.notice-report.json).")
    return p.parse_args()


_SEV_COLOR = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[37m"}
_STATUS_GLYPH = {"covered": "+", "partial": "~", "gap": "x", "not_applicable": "."}
_RESET = "\033[0m"


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if sys.stdout.isatty() else text


def _bar(c: int, pa: int, g: int, width: int = 40) -> str:
    total = max(1, c + pa + g)
    cc = round(width * c / total)
    pp = round(width * pa / total)
    gg = width - cc - pp
    return "[" + "+" * cc + "~" * pp + "x" * gg + "]"


def _print_finding(f: dict) -> None:
    glyph = _STATUS_GLYPH.get(f["status"], "?")
    sev = _c(_SEV_COLOR.get(f["severity"], ""), f"{f['severity']:<6}")
    src = "LLM" if f.get("verified") else ("N/A" if f["status"] == "not_applicable" else "sem")
    conf = f.get("confidence", 0.0)
    print(f"  {glyph} {f['reference']:<32} {sev} conf={conf:.2f} [{src}]  {f['title']}")
    ev = (f.get("evidence") or "").strip()
    rs = (f.get("reasoning") or "").strip()
    fx = (f.get("suggested_remediation") or "").strip()
    if ev:
        print(f"        evidence: \"{ev[:160]}\"")
    if rs:
        print(f"        reason:   {rs[:160]}")
    if fx and f["status"] in ("gap", "partial"):
        print(f"        fix:      {fx[:160]}")


def main() -> int:
    args = _parse_args()
    policy_path = Path(args.policy_file)
    if not policy_path.exists():
        print(f"ERROR: policy file not found: {policy_path}", file=sys.stderr)
        return 1
    policy_text = policy_path.read_text(encoding="utf-8")
    profile = {c.strip() for c in args.profile.split(",") if c.strip()}

    from src.rag.embeddings import SentenceTransformerEmbedder
    from src.rag.notice_analysis import NoticeAnalysisRefusal, analyze_notice

    embedder = SentenceTransformerEmbedder()

    # Decide mode.
    if args.no_llm:
        from src.llm.client import LLMError

        class _NoLLM:
            def complete(self, prompt): raise LLMError("LLM disabled (--no-llm)")
            def complete_json(self, prompt): raise LLMError("LLM disabled (--no-llm)")

        llm = _NoLLM()
        kwargs = {"verify_limit": 0, "verify_top_gaps": 0}
        mode_desc = "semantic only (no LLM)"
    else:
        from src.llm.client import OllamaClient
        llm = OllamaClient(
            model=CONFIG.llm_model,
            base_url=CONFIG.ollama_base_url,
            timeout_s=CONFIG.request_timeout_s,
            num_predict=1024,
        )
        if args.fast:
            kwargs = {}
            mode_desc = "hybrid (semantic auto-pass + LLM on borderline)"
        else:
            # RIGOROUS (default): verify every applicable requirement, strict grader.
            kwargs = {"verify_all": True, "strict": True,
                      "top_chunks": RIGOROUS_TOP_CHUNKS,
                      "verify_limit": 10_000, "verify_top_gaps": 10_000}
            mode_desc = "RIGOROUS (every requirement LLM-verified, strict grading)"

    print(f"--- analyzing {policy_path} ({len(policy_text):,} chars)")
    print(f"    profile: {sorted(profile) or '(only always-on requirements)'}")
    print(f"    mode:    {mode_desc}")
    if not args.no_llm and not args.fast:
        print("    note:    one LLM call per applicable requirement on CPU Mistral "
              "-- expect ~15-25 min. The first call also loads the model into RAM.")

    t0 = time.time()
    try:
        report = analyze_notice(policy_text, embedder=embedder, llm_client=llm,
                                org_profile=profile, **kwargs)
    except NoticeAnalysisRefusal as e:
        print(f"\nREFUSED: {e}", file=sys.stderr)
        return 1
    dt = time.time() - t0

    print(f"\n+++ {report.framework} ({report.jurisdiction}) -- complete in {dt:.1f}s")
    print(f"  applicable requirements: {report.n_requirements:>3}")
    print(f"  covered:                 {report.n_covered:>3}")
    print(f"  partial:                 {report.n_partial:>3}")
    print(f"  gap:                     {report.n_gap:>3}")
    print(f"  not applicable:          {report.n_not_applicable:>3}")
    print(f"  LLM verifications:       {report.n_llm_verifications:>3}")
    print(f"  {_bar(report.n_covered, report.n_partial, report.n_gap)}")

    fd = [f.to_dict() for f in report.findings]
    order = {"gap": 0, "partial": 1, "covered": 2, "not_applicable": 3}
    for label, st in (("GAPS", "gap"), ("PARTIAL", "partial"), ("COVERED", "covered")):
        rows = [f for f in fd if f["status"] == st]
        if rows:
            print(f"\n{label} ({len(rows)}):")
            for f in sorted(rows, key=lambda x: x["severity"]):
                _print_finding(f)
    na = [f for f in fd if f["status"] == "not_applicable"]
    if na:
        print(f"\nNOT APPLICABLE ({len(na)}): " + ", ".join(f["id"] for f in na))

    out_path = args.json_out or str(policy_path.with_suffix(policy_path.suffix + ".notice-report.json"))
    Path(out_path).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"\nFull machine-readable report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
