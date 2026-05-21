#!/usr/bin/env python3
"""
Generate a secure MCP API key.

Run this ONCE on your machine. It prints a key and the shell commands to
export it. The key is never written to a tracked file or into application
code -- you store it in your environment.

    python scripts/generate_api_key.py
"""
from __future__ import annotations

import secrets


def generate_api_key(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def main() -> None:
    key = generate_api_key()
    print("Generated MCP API key (shown once -- store it safely):\n")
    print(f"    {key}\n")
    print("Use it for THIS shell session:")
    print(f'    export PCT_MCP_API_KEY="{key}"\n')
    print("Persist it for future sessions (append to ~/.bashrc, never commit):")
    print(f"    echo 'export PCT_MCP_API_KEY=\"{key}\"' >> ~/.bashrc\n")
    print("Then start the server:")
    print("    python -m src.mcp_server.server")
    print("\nReminder: do NOT paste this key into .env that gets committed, "
          "into notebooks, or into source code.")


if __name__ == "__main__":
    main()
