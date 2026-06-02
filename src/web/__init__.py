"""Streamlit frontend for the Privacy Compliance Toolkit (v1.4).

Pure UI layer -- talks to the FastMCP HTTP server at PCT_MCP_API_URL via
fastmcp.Client. The toolkit's MCP layer, gateway, guardrails, and RAG engine
are untouched; this is a presentation skin so a human can see the analyst
work without writing Python.
"""
