"""
Streamlit frontend for the Privacy Compliance Toolkit (v1.4).

A pure UI layer -- talks to the FastMCP HTTP server at PCT_MCP_API_URL via
fastmcp.Client. The toolkit's MCP layer, gateway, guardrails, and RAG engine
are untouched; this is a presentation skin so a human can see the analyst
work without writing Python.

Run:
    streamlit run src/web/app.py

Prereqs:
    * the MCP server already running on http://127.0.0.1:8765
    * PCT_MCP_API_KEY exported (Streamlit picks it up automatically)
    * (for ask_compliance / analyze_policy) Ollama running with a model
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running as `streamlit run src/web/app.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from fastmcp import Client


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_SERVER_URL = os.environ.get("PCT_MCP_API_URL", "http://127.0.0.1:8765/mcp/")

STATUS_COLOR = {"covered": "#28a745", "partial": "#ffc107", "gap": "#dc3545"}
SEVERITY_COLOR = {"low": "#6c757d", "medium": "#ffc107", "high": "#dc3545"}

FRAMEWORKS = ["GDPR", "Danish DPA", "NIST CSF", "ISO 27701"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_tool_async(tool_name: str, args: dict, api_key: str, url: str) -> dict:
    """Open one fastmcp.Client, call the tool, return the parsed response dict."""
    async with Client(url, auth=api_key) as client:
        result = await client.call_tool(tool_name, args)
        text = result.content[0].text if result.content else "{}"
        return json.loads(text)


def call_tool(tool_name: str, args: dict) -> dict:
    """Sync wrapper -- each Streamlit button-click runs its own asyncio loop."""
    api_key = st.session_state.get("api_key", "")
    url = st.session_state.get("server_url", DEFAULT_SERVER_URL)
    if not api_key:
        raise RuntimeError(
            "No API key set. Paste your PCT_MCP_API_KEY into the sidebar."
        )
    return asyncio.run(_call_tool_async(tool_name, args, api_key, url))


def status_badge(status: str) -> str:
    color = STATUS_COLOR.get(status, "#6c757d")
    return (f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:6px;font-size:0.85em;font-weight:600;">{status.upper()}</span>')


def severity_badge(severity: str) -> str:
    color = SEVERITY_COLOR.get(severity, "#6c757d")
    return (f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:6px;font-size:0.85em;font-weight:600;">{severity.upper()}</span>')


def coverage_bar_html(n_covered: int, n_partial: int, n_gap: int) -> str:
    total = max(1, n_covered + n_partial + n_gap)
    pc = 100.0 * n_covered / total
    pp = 100.0 * n_partial / total
    pg = 100.0 * n_gap / total
    return (
        '<div style="display:flex;width:100%;height:36px;border-radius:6px;'
        'overflow:hidden;border:1px solid #ccc;font-size:0.8em;color:white;'
        'font-weight:600;text-align:center;line-height:36px;">'
        f'<div style="width:{pc}%;background:{STATUS_COLOR["covered"]};">'
        f'{f"covered {n_covered}" if pc >= 8 else ""}</div>'
        f'<div style="width:{pp}%;background:{STATUS_COLOR["partial"]};color:black;">'
        f'{f"partial {n_partial}" if pp >= 8 else ""}</div>'
        f'<div style="width:{pg}%;background:{STATUS_COLOR["gap"]};">'
        f'{f"gap {n_gap}" if pg >= 8 else ""}</div>'
        '</div>'
    )


def render_finding(f: dict, *, key: str | None = None) -> None:
    """Render one Finding as an expandable card."""
    title = f"{f['reference']} -- {f['requirement']}"
    with st.expander(title, expanded=False):
        c1, c2, c3 = st.columns([1, 1, 1])
        c1.markdown(f"**Status**<br>{status_badge(f['status'])}", unsafe_allow_html=True)
        c2.markdown(f"**Severity**<br>{severity_badge(f['severity'])}", unsafe_allow_html=True)
        c3.metric("Confidence", f"{f['confidence']:.2f}")
        if f.get("reasoning"):
            st.markdown(f"**Reasoning** — {f['reasoning']}")
        if f.get("evidence"):
            st.markdown("**Evidence excerpt**")
            st.info(f["evidence"])
        if f.get("suggested_remediation"):
            st.markdown(f"**Suggested fix** — {f['suggested_remediation']}")


def render_gap_report_single(out: dict, top: int) -> None:
    """Render an analyze_policy (single-framework) result."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Articles", out["n_articles"])
    c2.metric("Covered", out["n_covered"])
    c3.metric("Partial", out["n_partial"])
    c4.metric("Gap", out["n_gap"])
    st.markdown(coverage_bar_html(out["n_covered"], out["n_partial"], out["n_gap"]),
                unsafe_allow_html=True)
    st.caption(f"LLM verifications used: {out['n_llm_verifications']}")
    for status_label in ("gap", "partial", "covered"):
        findings = [f for f in out["findings"] if f["status"] == status_label][:top]
        if findings:
            st.markdown(f"### Top {status_label.upper()} findings")
            for i, f in enumerate(findings):
                render_finding(f, key=f"{status_label}_{i}")


def render_gap_report_all(out: dict, top: int) -> None:
    """Render an analyze_policy_all (multi-framework) result."""
    c1, c2 = st.columns(2)
    c1.metric("Articles examined", out["n_articles"])
    c2.metric("LLM verifications", out["n_llm_verifications"])
    for entry in out["per_framework"]:
        st.markdown(f"### {entry['framework']}")
        st.markdown(coverage_bar_html(entry["n_covered"], entry["n_partial"], entry["n_gap"]),
                    unsafe_allow_html=True)
        st.caption(f"{entry['n_articles']} articles, "
                   f"{entry['n_covered']} covered, "
                   f"{entry['n_partial']} partial, "
                   f"{entry['n_gap']} gap")
        for status_label in ("gap", "partial", "covered"):
            findings = [f for f in entry["findings"] if f["status"] == status_label][:top]
            if findings:
                st.markdown(f"##### Top {status_label.upper()}")
                for i, f in enumerate(findings):
                    render_finding(f, key=f"{entry['framework']}_{status_label}_{i}")
        st.divider()


# ---------------------------------------------------------------------------
# Page config + sidebar
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Privacy Compliance Toolkit",
    page_icon="🔒",
    layout="wide",
)


with st.sidebar:
    st.markdown("# 🔒 Privacy Toolkit")
    st.caption("Security-by-design AI for privacy & compliance")
    st.divider()

    # Auto-pick from env vars if present; let the user override in the UI.
    if "api_key" not in st.session_state:
        st.session_state.api_key = os.environ.get("PCT_MCP_API_KEY", "")
    if "server_url" not in st.session_state:
        st.session_state.server_url = DEFAULT_SERVER_URL

    st.text_input("API key", key="api_key", type="password",
                  help="Bearer token. Auto-populated from PCT_MCP_API_KEY env var.")
    st.text_input("Server URL", key="server_url",
                  help="MCP server endpoint. Default: localhost:8765.")

    st.divider()
    st.markdown(
        "**What this is**\n\n"
        "A security-by-design privacy & compliance toolkit covering "
        "**GDPR**, **Danish DPA**, **NIST CSF 2.0**, and **ISO/IEC 27701** "
        "(279 articles total), with verified citations and a local LLM.\n\n"
        "**7 MCP tools** are exposed; each tab here calls one.\n\n"
        "Built by Kumari Rupali Bansal."
    )


# ---------------------------------------------------------------------------
# Header + Tabs
# ---------------------------------------------------------------------------

st.title("🔒 Privacy Compliance Toolkit")
st.caption(
    "279 articles across 4 frameworks · 7 MCP tools · verified citations · "
    "runs locally with no cloud dependencies"
)

tab_home, tab_fws, tab_lookup, tab_search, tab_semantic, tab_ask, tab_gap = st.tabs([
    "🏠 Home",
    "📚 Frameworks",
    "📖 Look up article",
    "🔍 Keyword search",
    "🧠 Semantic search",
    "💬 Ask compliance",
    "🎯 Gap analysis",
])


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
with tab_home:
    st.markdown("""
## What's unusual about this toolkit

Most "AI for compliance" demos wire an LLM directly to a vector store and call
it a day. This project takes the opposite stance:

- **Every data access is audited.** SQLite *and* the vector store sit behind
  a logging gateway; the audit row lands before the data is touched, fsync'd,
  fail-loud.
- **Every LLM citation is verified.** The LLM emits citations as a structured
  JSON field. Every citation must trace back to both the retrieved rules AND
  the loaded framework rows. Fabrications refuse the entire response rather
  than passing through.
- **Every guardrail is adversarially tested.** SSRF, injection, PII redaction,
  citation hallucination, auth bypass, rate-limit bypass -- each one has an
  attack written against it and a refusal verified. See `docs/SECURITY-REVIEW.md`
  in the repo.
- **Local & free by default.** Embeddings (sentence-transformers, MIT) and LLM
  (Ollama + Mistral 7B) run on your machine. No cloud, no API keys for the AI
  parts.

## Pick a tab above to try it

The 7 tabs each exercise one MCP tool the server exposes. The headline demo is
the **Gap analysis** tab -- paste a privacy policy, pick a framework, get a
per-requirement coverage report.

## Performance notes

Tools that call the LLM (`ask_compliance`, `analyze_policy`,
`analyze_policy_all`) take real time on a CPU because Mistral 7B has to think:

| Tool | Time on CPU |
|------|-------------|
| `list_frameworks` / `get_article` / `search_frameworks` | sub-second |
| `semantic_search` | ~1-3 s after first call (sentence-transformers warms up) |
| `ask_compliance` | ~30-90 s (one LLM call) |
| `analyze_policy` (single framework) | ~5-15 min (~10 LLM calls) |
| `analyze_policy_all` (all 4) | ~20-60 min |

That's why some tabs show a long spinner. Be patient; it's not stuck.
""")


# ---------------------------------------------------------------------------
# Frameworks
# ---------------------------------------------------------------------------
with tab_fws:
    st.markdown("### Loaded frameworks")
    st.caption("Calls `list_frameworks`. Reads SQLite via the audited gateway.")
    if st.button("List frameworks", type="primary", key="fws_btn"):
        try:
            with st.spinner("Reading frameworks..."):
                out = call_tool("list_frameworks", {})
            if out.get("ok"):
                cols = st.columns(len(out["frameworks"]))
                for col, fw in zip(cols, out["frameworks"]):
                    col.metric(
                        label=f"{fw['name']}",
                        value=f"{fw['article_count']} articles",
                        delta=f"v{fw['version']}",
                        delta_color="off",
                    )
            else:
                st.error(out.get("reason", "Unknown error"))
        except Exception as e:
            st.error(f"Call failed: {e}")


# ---------------------------------------------------------------------------
# Look up article
# ---------------------------------------------------------------------------
with tab_lookup:
    st.markdown("### Look up an article by its reference")
    st.caption("Calls `get_article`. Useful for spot-checking a citation the toolkit gave you.")
    c1, c2 = st.columns([1, 2])
    framework = c1.selectbox("Framework", FRAMEWORKS, key="lookup_fw")
    reference = c2.text_input("Reference", placeholder="GDPR Art. 6", key="lookup_ref")
    if st.button("Look up", type="primary", key="lookup_btn"):
        try:
            with st.spinner("Fetching..."):
                out = call_tool("get_article", {"framework": framework, "reference": reference})
            if out.get("ok"):
                st.markdown(f"#### {out['framework']} — {out['reference']}")
                st.markdown(f"**{out['requirement']}**")
                st.caption(out.get("category", ""))
                st.write(out["body"])
            else:
                st.error(out.get("reason", "Unknown error"))
        except Exception as e:
            st.error(f"Call failed: {e}")


# ---------------------------------------------------------------------------
# Keyword search
# ---------------------------------------------------------------------------
with tab_search:
    st.markdown("### Keyword search across all frameworks")
    st.caption("Calls `search_frameworks`. SQL `LIKE` matching on body / requirement / category.")
    c1, c2 = st.columns([3, 1])
    query = c1.text_input("Query", placeholder="consent", key="search_q")
    limit = c2.number_input("Max results", min_value=1, max_value=50, value=10, key="search_lim")
    if st.button("Search", type="primary", key="search_btn"):
        try:
            with st.spinner("Searching..."):
                out = call_tool("search_frameworks", {"query": query, "limit": int(limit)})
            if out.get("ok"):
                st.success(f"{out['count']} result(s) for {out['query']!r}")
                for i, r in enumerate(out["results"]):
                    title = f"{r['framework']} {r['reference']} -- {r['requirement']}"
                    with st.expander(title):
                        st.caption(r.get("category", ""))
                        st.write(r["snippet"])
            else:
                st.error(out.get("reason", "Unknown error"))
        except Exception as e:
            st.error(f"Call failed: {e}")


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------
with tab_semantic:
    st.markdown("### Semantic search — meaning, not keywords")
    st.caption(
        "Calls `semantic_search`. ChromaDB + sentence-transformers ranks by meaning. "
        "*\"when am I allowed to use someone's data\"* ranks GDPR Art. 6 high without "
        "the words *consent* or *Art. 6* ever appearing in the query."
    )
    c1, c2, c3 = st.columns([3, 1, 1])
    query = c1.text_input("Free-text query",
                          placeholder="when am I allowed to use someone's data",
                          key="sem_q")
    k = c2.number_input("Top-k", min_value=1, max_value=20, value=5, key="sem_k")
    framework = c3.selectbox("Framework", ["all"] + FRAMEWORKS, key="sem_fw")
    if st.button("Search", type="primary", key="sem_btn"):
        try:
            args = {"query": query, "k": int(k)}
            if framework != "all":
                args["framework"] = framework
            with st.spinner("Embedding + searching..."):
                out = call_tool("semantic_search", args)
            if out.get("ok"):
                st.success(f"{out['count']} result(s)")
                for i, r in enumerate(out["results"]):
                    score = r.get("score", 0)
                    title = (f"{r['framework']} {r['reference']} "
                             f"-- {r['requirement']}  (score {score:.3f})")
                    with st.expander(title):
                        st.caption(r.get("category", ""))
                        st.write(r["snippet"])
            else:
                st.error(out.get("reason", "Unknown error"))
        except Exception as e:
            st.error(f"Call failed: {e}")


# ---------------------------------------------------------------------------
# Ask compliance
# ---------------------------------------------------------------------------
with tab_ask:
    st.markdown("### Ask a compliance question — RAG with citation enforcement")
    st.caption(
        "Calls `ask_compliance`. Retrieves rules → asks Mistral → "
        "**every citation Mistral emits must trace back to the retrieved rules AND the "
        "loaded framework rows.** Hallucinated citations refuse the entire response."
    )
    question = st.text_area(
        "Question",
        placeholder="What is the lawful basis for processing personal data under GDPR?",
        height=80,
        key="ask_q",
    )
    framework = st.selectbox("Framework (optional)",
                             ["all"] + FRAMEWORKS, key="ask_fw")
    if st.button("Ask", type="primary", key="ask_btn"):
        try:
            args = {"question": question}
            if framework != "all":
                args["framework"] = framework
            with st.spinner("Retrieving rules + Mistral is thinking... ~30-90 s on CPU"):
                out = call_tool("ask_compliance", args)
            if out.get("ok"):
                st.markdown("### Answer")
                st.write(out["answer"])
                c1, c2 = st.columns(2)
                c1.metric("Confidence", f"{out['confidence']:.2f}")
                c2.metric("Citations", len(out["citations"]))
                if out["citations"]:
                    st.markdown("### Citations (each verified to trace back)")
                    for c in out["citations"]:
                        st.markdown(f"- **{c['framework']} {c['reference']}**")
                with st.expander("What was retrieved before generation"):
                    st.code("\n".join(out.get("retrieved_refs", [])))
            else:
                st.error(out.get("reason", "Unknown error"))
        except Exception as e:
            st.error(f"Call failed: {e}")


# ---------------------------------------------------------------------------
# Gap analysis -- the showcase
# ---------------------------------------------------------------------------
with tab_gap:
    st.markdown("### Privacy-policy gap analysis — the showcase 🎯")
    st.caption(
        "Calls `analyze_policy` (or `analyze_policy_all` for the all-four mode). "
        "Paste a privacy policy → get per-requirement findings: "
        "**covered / partial / gap** with severity, reasoning, suggested remediation. "
        "Hybrid pipeline: fast semantic scoring for clear cases + Mistral verifying "
        "borderline ones."
    )

    policy = st.text_area(
        "Privacy policy text",
        height=300,
        placeholder="Paste a privacy policy here (e.g. from any company's privacy page)...",
        help="Free-text policy, up to ~50,000 characters.",
        key="gap_policy",
    )
    c1, c2 = st.columns([2, 1])
    framework_choice = c1.selectbox(
        "Framework",
        FRAMEWORKS + ["All four (slow)"],
        key="gap_fw",
    )
    top = c2.number_input(
        "Top findings to show (per status)",
        min_value=1, max_value=20, value=5, key="gap_top",
    )

    st.info(
        "Single framework on CPU Mistral: ~5-15 min. All four: 20-60 min. "
        "Be patient; the spinner will keep updating until done."
    )

    if st.button("Analyze policy", type="primary", key="gap_btn"):
        if not policy.strip():
            st.error("Paste a policy first.")
        else:
            try:
                use_all = framework_choice == "All four (slow)"
                if use_all:
                    with st.spinner("Analyzing across all 4 frameworks (~20-60 min)..."):
                        out = call_tool("analyze_policy_all", {"policy_text": policy})
                else:
                    with st.spinner(
                        f"Analyzing against {framework_choice} (~5-15 min)..."
                    ):
                        out = call_tool(
                            "analyze_policy",
                            {"policy_text": policy, "framework": framework_choice},
                        )
                if not out.get("ok"):
                    st.error(out.get("reason", "Unknown error"))
                else:
                    if use_all:
                        render_gap_report_all(out, int(top))
                    else:
                        render_gap_report_single(out, int(top))
            except Exception as e:
                st.error(f"Call failed: {e}")
