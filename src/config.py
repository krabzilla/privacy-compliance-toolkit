"""
Configuration with secure defaults.

Principle: secure-by-default means changing nothing should give you the safest
behaviour. Every knob here defaults to the conservative value; .env can loosen
them only with intent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration. Read once at startup."""

    # ---- Paths ----
    data_dir: Path
    db_path: Path
    audit_log_path: Path
    frameworks_dir: Path

    # ---- LLM ----
    openai_api_key: str
    openai_model: str
    llm_temperature: float
    llm_max_tokens: int

    # ---- Guardrails ----
    max_file_size_mb: int
    max_url_fetch_bytes: int
    request_timeout_s: int
    confidence_threshold: float

    # ---- MCP ----
    mcp_host: str
    mcp_port: int

    # ---- MCP auth / rate limiting ----
    # The API key is NEVER hard-coded. It is read from the environment only.
    # Generate one with scripts/generate_api_key.py and export PCT_MCP_API_KEY.
    mcp_api_key: str
    mcp_rate_limit_requests: int
    mcp_rate_limit_window_s: int

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_env("PCT_DATA_DIR", "./data")).resolve()
        return cls(
            data_dir=data_dir,
            db_path=Path(_env("PCT_DB_PATH", str(data_dir / "toolkit.db"))).resolve(),
            audit_log_path=Path(
                _env("PCT_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
            ).resolve(),
            frameworks_dir=(data_dir / "frameworks").resolve(),
            openai_api_key=_env("OPENAI_API_KEY", ""),
            openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
            llm_temperature=_env_float("PCT_LLM_TEMPERATURE", 0.3),
            llm_max_tokens=_env_int("PCT_LLM_MAX_TOKENS", 2000),
            max_file_size_mb=_env_int("PCT_MAX_FILE_SIZE_MB", 10),
            max_url_fetch_bytes=_env_int("PCT_MAX_URL_FETCH_BYTES", 5_000_000),
            request_timeout_s=_env_int("PCT_REQUEST_TIMEOUT_S", 30),
            confidence_threshold=_env_float("PCT_CONFIDENCE_THRESHOLD", 0.75),
            mcp_host=_env("PCT_MCP_HOST", "127.0.0.1"),
            mcp_port=_env_int("PCT_MCP_PORT", 8765),
            mcp_api_key=_env("PCT_MCP_API_KEY", ""),
            mcp_rate_limit_requests=_env_int("PCT_MCP_RATE_LIMIT_REQUESTS", 60),
            mcp_rate_limit_window_s=_env_int("PCT_MCP_RATE_LIMIT_WINDOW_S", 60),
        )


# Module-level singleton -- import this everywhere, do not re-read env elsewhere.
CONFIG = Config.from_env()
