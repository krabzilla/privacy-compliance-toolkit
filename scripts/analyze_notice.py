#!/usr/bin/env python3
"""
analyze_notice.py -- grade a privacy NOTICE against the GDPR notice checklist.

Unlike analyze_policy.py (which grades against the full 99-article framework via
the MCP server), this runs the v1.5 CHECKLIST analyzer IN-PROCESS -- no server,
no API key. It scores the policy only against the disclosures a public notice
must actually make (GDPR Arts. 12-14 + the Danish CPR overlay), filtered by the
org profile you declare.

Prereqs:
    * pip install -r requirements.txt   (sentence-transformers, pyyaml, httpx)
    * for LLM verification of borderline items: Ollama running with the model
      in PCT_LLM_MODEL (default mistral:7b-instruct). Without it, borderline
      items are flagged for human review; clear covered/gap items still resolve.

Usage:
    python scripts/analyze_notice.py my_policy.txt
    python scripts/analyze_notice.py my_policy.txt \
        --profile data_collected_directly,legal_basis_includes_consent,transfers_outside_eea
    python scripts/analyze_notice.py my_policy.txt --no-llm     # semantic only
    python scripts/analyze_notice.py my_policy.txt --json out.json

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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Grade a privacy notice against the GDPR notice checklist.")
    p.add_argument("policy_file", help="Path to the policy/notice text file.")
    p.add_argument(
        "--profile",
        default="",
        help="Comma-separated org-profile facts (default: none -> only 'always' "
             "requirements are graded).",
    )
    p.add_argument("--no-llm", action="store_true",
                   help="Skip LLM verification; use semantic coverage only (fast).")
    p.add_argument("--top", type=int, default=8, help="How many gap findings to print.")
    p.add_argument("--json", dest="json_out", default=None,
                   help="Also write the full report JSON to this path.")
    return p.parse_args()


_SEV_COLOR = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[37m"}
_STATUS_GLYPH = {"covered": "+", "partial": "~", "gap": "x", "not_applicable": "."}
_RESET = "\033[0m"


def _sev(s: str) -> str:
    if not sys.stdout.isatty():
        return f"{s:<6}"
    return f"{_SEV_COLOR.get(s, '')}{s:<6}{_RESET}"


def _bar(c: int, p: int, g: int, width: int = 40) -> str:
    total = max(1, c + p + g)
    cc = round(width * c / total)
    pp = round(width * p / total)
    gg = width - cc - pp
    return "[" + "+" * cc + "~" * pp + "x" * gg + "]"


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

    if args.no_llm:
        # A do-nothing client that always raises -> no requirement is ever sent
        # to a model. We also set verify limits to 0 so the queue is empty.
        from src.llm.client import LLMError

        class _NoLLM:
            def complete(self, prompt): raise LLMError("LLM disabled (--no-llm)")
            def complete_json(self, prompt): raise LLMError("LLM disabled (--no-llm)")

        llm = _NoLLM()
        verify_kwargs = {"verify_limit": 0, "verify_top_gaps": 0}
    else:
        from src.llm.client import OllamaClient
        # Bound generation so a slow model can't eat the whole timeout. Our JSON
        # replies are tiny; 1024 output tokens is plenty.
        llm = OllamaClient(
            model=CONFIG.llm_model,
            base_url=CONFIG.ollama_base_url,
            timeout_s=CONFIG.request_timeout_s,
            num_predict=1024,
        )
        verify_kwargs = {}

    print(f"--- analyzing {policy_path} ({len(policy_text):,} chars)")
    print(f"    profile: {sorted(profile) or '(only always-on requirements)'}")
    if not args.no_llm:
        print("    (borderline items are LLM-verified one at a time; first run "
              "loads the model into RAM)")

    t0 = time.time()
    try:
        report = analyze_notice(
            policy_text,
            embedder=embedder,
            llm_client=llm,
            org_profile=profile,
            **verify_kwargs,
        )
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

    gaps = [f for f in report.findings if f.status == "gap"][:args.top]
    partials = [f for f in report.findings if f.status == "partial"][:args.top]
    if gaps:
        print("\nGAPS (missing disclosures):")
        for f in gaps:
            print(f"  {f.reference:<34} {_sev(f.severity)}  {f.title}")
            if f.suggested_remediation:
                print(f"      fix: {f.suggested_remediation[:140]}")
    if partials:
        print("\nPARTIAL / needs review:")
        for f in partials:
            print(f"  {f.reference:<34} {_sev(f.severity)}  {f.title}")
            if (f.reasoning or "").strip():
                print(f"      {f.reasoning[:140]}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
