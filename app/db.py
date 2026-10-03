"""SQL Server access: pooled, read-only execution with timeouts + live catalog."""
from __future__ import annotations

import datetime as dt
import decimal
import logging
import urllib.parse
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from .config import settings

log = logging.getLogger(__name__)


def _odbc_string() -> str:
    return (
        f"DRIVER={{{settings.db_driver}}};SERVER={settings.db_server};"
        f"DATABASE={settings.db_name};UID={settings.db_user};PWD={settings.db_password};"
        "TrustServerCertificate=yes;Encrypt=yes;ApplicationIntent=ReadOnly;"
    )


@lru_cache(maxsize=1)
def get_engine():
    url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(_odbc_string())
    return create_engine(
        url,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"timeout": 15},  # login timeout
    )


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>"
    return str(v)


def run_select(sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Execute a (pre-validated) SELECT. Returns columns, rows, truncated flag."""
    max_rows = max_rows or settings.max_rows
    raw = get_engine().raw_connection()
    try:
        drv = raw.driver_connection
        drv.timeout = settings.db_query_timeout  # pyodbc query timeout (seconds)
        cur = raw.cursor()
        cur.execute("SET LOCK_TIMEOUT 10000; SET NOCOUNT ON;")
        while cur.nextset():  # drain
            pass
        cur.execute(sql)
        columns = [c[0] for c in cur.description] if cur.description else []
        fetched = cur.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [[_json_safe(v) for v in r] for r in fetched[:max_rows]]
        return {"columns": columns, "rows": rows, "truncated": truncated}
    finally:
        try:
            raw.rollback()
        finally:
            raw.close()


def fetch_catalog() -> dict[str, dict[str, str]]:
    """Live INFORMATION_SCHEMA -> {table_lower: {column_lower: data_type}}."""
    sql = (
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'dbo'"
    )
    out: dict[str, dict[str, str]] = {}
    with get_engine().connect() as conn:
        for t, c, d in conn.execute(text(sql)):
            out.setdefault(t.lower(), {})[c.lower()] = d.lower()
    return out


def fetch_distinct(table: str, column: str, limit: int = 15) -> list[str]:
    """Distinct values of a low-cardinality column (identifiers are pre-validated)."""
    sql = (
        f"SELECT TOP {int(limit) + 1} CAST([{column}] AS NVARCHAR(60)) v, COUNT(*) n "
        f"FROM [dbo].[{table}] WHERE [{column}] IS NOT NULL GROUP BY [{column}] ORDER BY n DESC"
    )
    with get_engine().connect() as conn:
        vals = [r[0] for r in conn.execute(text(sql))]
    return vals if len(vals) <= limit else []  # high-cardinality -> not an enum


def ping() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        log.error("DB ping failed: %s", e)
        return False


def resolve_member(ident: str) -> dict[str, str] | None:
    """Verify the caller against vMember (usercode OR userid). Returns None if unknown."""
    sql = text("SELECT TOP 1 usercode, userid, name FROM dbo.vMember WHERE usercode = :i OR userid = :i")
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"i": ident}).first()
    if not row:
        return None
    code, uid, name = [(str(x).strip() if x is not None else "") for x in row]
    return {"usercode": code, "userid": uid, "name": name}
