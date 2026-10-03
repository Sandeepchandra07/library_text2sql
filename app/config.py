"""Central configuration – everything comes from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    db_server: str = os.getenv("DB_SERVER", "")
    db_user: str = os.getenv("DB_USER", "")
    db_password: str = os.getenv("DB_PASSWORD", "")
    db_name: str = os.getenv("DB_NAME", "")
    db_driver: str = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server")
    db_query_timeout: int = _int("DB_QUERY_TIMEOUT", 30)
    max_rows: int = _int("MAX_ROWS", 200)

    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
    llm_timeout: int = _int("LLM_TIMEOUT", 120)

    max_sql_retries: int = _int("MAX_SQL_RETRIES", 2)
    default_inst_id: str = os.getenv("DEFAULT_INST_ID", "").strip()
    api_key: str = os.getenv("API_KEY", "").strip()
    memory_db: str = os.getenv("MEMORY_DB", str(BASE_DIR / "data" / "memory.sqlite3"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    data_dir: Path = BASE_DIR / "data"


settings = Settings()
