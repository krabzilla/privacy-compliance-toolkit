#!/usr/bin/env python3
"""
analyze_policy.py -- run gap analysis on any privacy-policy file.

Hands a policy file to the running Privacy Compliance Toolkit MCP server,
calls analyze_policy (single framework) or analyze_policy_all (every
loaded framework), and prints a structured summary + top findings.

Prereqs:
    * the MCP server running on http://127.0.0.1:8765 with v1.3 tools
      registered (analyze_policy / analyze_policy_all)
    * PCT_MCP_API_KEY exported in this shell
    * (for verification) Ollama running with a model installed

Usage:
    python scripts/analyze_policy.py                              # sample policy, GDPR
    python scripts/analyze_policy.py my_policy.txt                # custom file, GDPR
    python scripts/analyze_policy.py my_policy.txt --framework "ISO 27701"
    python scripts/analyze_policy.py my_policy.txt --all          # every framework
    python scripts/analyze_policy.py my_policy.txt --top 10       # show more findings

Tip: copy the visible text of any company's privacy policy from their
website into a .txt file and run this script against it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Allow running as `python scripts/analyze_policy.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402


SERVER_URL = "http://127.0.0.1:8765/mcp/"
DEFAULT_POLICY = "data/examples/sample_policy.md"
DEFAULT_FRAMEWORK = "GDPR"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run privacy-policy gap analysis against the toolkit.",
    )
    p.add_argument(
        "policy_file",
        nargs="?",
        default=DEFAULT_POLICY,
        help=f"Path to the policy file (default: {DEFAULT_POLICY}).",
    )
    p.add_argument(
        "--framework",
        default=DEFAULT_FRAMEWORK,
        help='Framework to analyze against (e.g. "GDPR", "ISO 27701", '
             '"Danish DPA", "NIST CSF"). Ignored when --all is set. '
             f'Default: {DEFAULT_FRAMEWORK}.',
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Analyze against EVERY loaded framework at once (uses "
             "analyze_policy_all; slower, but the full picture).",
    )
    p.add_argument(
        "--top",
        type=int,
        default=5,
        help="How many top gap and partial findings to show (default: 5).",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Pretty-printers
# ---------------------------------------------------------------------------

_SEV_COLOR = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[37m"}
_RESET = "\033[0m"


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _sev(s: str) -> str:
    if not _is_tty():
        return s
    return f"{_SEV_COLOR.get(s, '')}{s:<6}{_RESET}"


def _bar(n_covered: int, n_partial: int, n_gap: int, width: int = 40) -> str:
    total = max(1, n_covered + n_partial + n_gap)
    c = round(width * n_covered / total)
    p = round(width * n_partial / total)
    g = width - c - p
    return "[" + "+" * c + "~" * p + "x" * g + "]"


def _print_finding(f: dict, *, indent: str = "  ") -> None:
    """Print one finding compactly."""
    sev = _sev(f["severity"])
    print(f"{indent}{f['reference']:<22} {sev}  {f['requirement'][:60]}")
    reason = (f.get("reasoning") or "").strip()
    if reason:
        print(f"{indent}    {reason[:120]}")
    rem = (f.get("suggested_remediation") or "").strip()
    if rem and rem.lower() not in ("", "n/a"):
        print(f"{indent}    fix: {rem[:120]}")


def _print_per_framework_summary(per_fw: list[dict], top: int) -> None:
    for entry in per_fw:
        print()
        print(f"--- {entry['framework']} ---")
        print(f"  articles: {entry['n_articles']:>4}   "
              f"covered: {entry['n_covered']:>3}   "
              f"partial: {entry['n_partial']:>3}   "
              f"gap: {entry['n_gap']:>3}")
        print(f"  {_bar(entry['n_covered'], entry['n_partial'], entry['n_gap'])}")
        gaps = [f for f in entry["findings"] if f["status"] == "gap"][:top]
        if gaps:
            print("\n  Top gap findings:")
            for f in gaps:
                _print_finding(f, indent="    ")


def _print_single_framework(out: dict, top: int) -> None:
    print(f"  articles examined:   {out['n_articles']:>4}")
    print(f"  covered:             {out['n_covered']:>4}")
    print(f"  partial:             {out['n_partial']:>4}")
    print(f"  gap:                 {out['n_gap']:>4}")
    print(f"  LLM verifications:   {out['n_llm_verifications']:>4}")
    print(f"  {_bar(out['n_covered'], out['n_partial'], out['n_gap'])}")

    findings = out.get("findings", [])
    gaps = [f for f in findings if f["status"] == "gap"][:top]
    partials = [f for f in findings if f["status"] == "partial"][:top]
    covered = [f for f in findings if f["status"] == "covered"][:3]

    if gaps:
        print("\nTop GAP findings:")
        for f in gaps:
            _print_finding(f)
    if partials:
        print("\nTop PARTIAL findings:")
        for f in partials:
            _print_finding(f)
    if covered:
        print("\nA few COVERED findings (for context):")
        for f in covered:
            _print_finding(f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    args = _parse_args()

    key = os.environ.get("PCT_MCP_API_KEY")
    if not key:
        print("ERROR: PCT_MCP_API_KEY is not set. Export it first.", file=sys.stderr)
        return 1

    policy_path = Path(args.policy_file)
    if not policy_path.exists():
        print(f"ERROR: policy file not found: {policy_path}", file=sys.stderr)
        return 1
    policy_text = policy_path.read_text(encoding="utf-8")
    print(
        f"--- analyzing {policy_path}"
        f"  ({len(policy_text):,} chars)"
        f"  against {'EVERY loaded framework' if args.all else args.framework!r}"
        f"  via {SERVER_URL}"
    )
    print("(This typically takes 1-3 minutes -- the LLM verifies borderline "
          "articles one at a time. Be patient on the first run; Ollama is "
          "loading Mistral into RAM.)")

    t0 = time.time()
    async with Client(SERVER_URL, auth=key) as client:
        if args.all:
            result = await client.call_tool(
                "analyze_policy_all", {"policy_text": policy_text}
            )
        else:
            result = await client.call_tool(
                "analyze_policy",
                {"policy_text": policy_text, "framework": args.framework},
            )
    dt = time.time() - t0

    text = result.content[0].text if result.content else "{}"
    out = json.loads(text)

    if not out.get("ok", False):
        print(f"\nREFUSED ({dt:.1f}s):  {out.get('reason', out.get('error', 'unknown'))}",
              file=sys.stderr)
        return 1

    print(f"\n+++ Analysis complete in {dt:.1f}s")
    if args.all:
        print(f"  total articles examined: {out.get('n_articles', '?')}")
        print(f"  LLM verifications used:  {out.get('n_llm_verifications', '?')}")
        _print_per_framework_summary(out.get("per_framework", []), args.top)
    else:
        _print_single_framework(out, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
