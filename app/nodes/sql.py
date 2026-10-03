"""Nodes 4-6: generate_sql -> execute_sql -> (retry_generate | END | generate_chart)"""
from __future__ import annotations

import datetime as dt
import logging
import time

from .. import domain, llm, memory, sql_guard
from ..config import settings
from ..runtime import get_kb
from ..schema_kb import tokenize
from ..state import GraphState

log = logging.getLogger(__name__)

_SYSTEM = """You are a senior Microsoft SQL Server (T-SQL) analyst for a library management system.
Translate the user's question into ONE correct, read-only T-SQL query.

{rules}

=== SCHEMA (only these tables/columns exist) ===
{schema}

=== BUSINESS RULES that apply to this question ===
{business}

{security}Today's date: {today}.

=== EXAMPLES (same database) ===
{examples}
"""


def _pick_examples(question: str, state: GraphState, k: int = 4) -> str:
    qt = set(tokenize(question))
    member = state.get("role") == "member"
    scored = []
    for eq, esql in domain.EXAMPLES:
        if "CALLER_" in esql:
            if not member:
                continue
            esql = esql.replace("CALLER_CODE", state.get("member_code", "")).replace("CALLER_KEY", state.get("member_key", ""))
        et = set(tokenize(eq))
        scored.append((len(qt & et) / (len(qt | et) or 1), eq, esql))
    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(f"Q: {eq}\n```sql\n{esql}\n```" for _, eq, esql in scored[:k])


def _security_block(state: GraphState) -> str:
    parts = []
    if state.get("role") == "member":
        code, key = state.get("member_code", ""), state.get("member_key", "")
        parts.append(
            f"ROW-LEVEL SECURITY: the caller is the MEMBER '{state.get('member_name', '')}' (member code '{code}', user id '{key}'). "
            f"'my / mera / meri' = this member. Profile, fine balance, validity, issued-book count => view vMember with "
            f"WHERE usercode = '{code}'. Loans/returns => CircIssueTransaction / CircReceiveTransaction with WHERE userid = '{key}'. "
            f"Fine_calculation.userid = '{key}'; Circpartpayment.user_id = '{key}'. The filter must be a top-level AND condition. "
            f"NEVER return another member's data and never list members.")
    if settings.default_inst_id:
        parts.append(f"MULTI-INSTITUTE: also filter InstId = {settings.default_inst_id} on tables that have an InstId column.")
    return ("\n".join(parts) + "\n\n") if parts else ""


def _build_system(state: GraphState) -> str:
    return _SYSTEM.format(
        rules=domain.GLOBAL_SQL_RULES, schema=state.get("schema_context", ""),
        business=state.get("rules_context") or "(none matched - use the schema and examples)",
        security=_security_block(state), today=dt.date.today().isoformat(),
        examples=_pick_examples(state["standalone_question"], state))


def generate_sql(state: GraphState) -> dict:
    t0 = time.perf_counter()
    u = state.get("understanding", {})
    if state.get("sql_template"):
        log.info("generate_sql | KPI template | %s", state["sql_template"])
        return {"sql": state["sql_template"], "timings": {**state.get("timings", {}), "generate_sql": round(time.perf_counter() - t0, 3)}}
    if state.get("role") == "member" and u.get("self_fields"):   # built by code, not by the LLM
        cols = ", ".join(f"{domain.SELF_FIELDS[f][0]} AS {domain.SELF_FIELDS[f][1]}" for f in u["self_fields"] if f in domain.SELF_FIELDS)
        code = str(state.get("member_code", "")).replace("'", "''")
        sql = f"SELECT {cols} FROM vMember WHERE {state.get('member_col', 'usercode')} = '{code}'"
        log.info("generate_sql | self-profile template | %s", sql)
        return {"sql": sql, "timings": {**state.get("timings", {}), "generate_sql": round(time.perf_counter() - t0, 3)}}
    user = (f"Question: {state['standalone_question']}\n"
            f"Operation: {u.get('operation', '')}; filters: {u.get('filters', {})}; assumptions: {u.get('assumptions', [])}\n"
            "Write the T-SQL query. Reply with only the SQL inside a ```sql block.")
    sql = llm.extract_sql(llm.chat(_build_system(state), user, max_tokens=900))
    log.info("generate_sql | %s", sql.replace("\n", " ")[:400])
    return {"sql": sql, "timings": {**state.get("timings", {}), "generate_sql": round(time.perf_counter() - t0, 3)}}


def retry_generate(state: GraphState) -> dict:
    t0 = time.perf_counter()
    zero = bool(state.get("zero_retry_pending"))
    if zero:
        problem = ("The query ran but returned 0 rows. Filters may be too strict: shorten LIKE keywords (drop one word or use the "
                   "most distinctive word), check status/flag literals against the 'values:' lists, and do not add extra conditions. "
                   "If the data genuinely cannot exist, return the same query.")
    else:
        problem = f"The query FAILED with this error:\n{state.get('exec_error', '')}"
    user = (f"Question: {state['standalone_question']}\n\nPrevious SQL:\n```sql\n{state.get('sql', '')}\n```\n\n{problem}\n\n"
            "Return a corrected T-SQL query only, inside a ```sql block. Use only columns that exist in the SCHEMA.")
    sql = llm.extract_sql(llm.chat(_build_system(state), user, max_tokens=900))
    log.info("retry_generate #%s | %s", state.get("attempts", 0) + 1, sql.replace("\n", " ")[:400])
    return {"sql": sql, "attempts": state.get("attempts", 0) + 1, "exec_error": "", "zero_retry_pending": False,
            "zero_retry_done": state.get("zero_retry_done", False) or zero,
            "timings": {**state.get("timings", {}), f"retry_{state.get('attempts', 0) + 1}": round(time.perf_counter() - t0, 3)}}


_RETRYABLE = ("invalid column", "invalid object", "incorrect syntax", "conversion failed", "ambiguous column", "error converting",
              "must appear in the group by", "divide by zero", "arithmetic overflow", "timeout expired", "hyt00",
              "is not a recognized", "argument data type", "operand type clash", "multi-part identifier")
_FATAL_MSG = {
    "en": "Sorry, I couldn't get a reliable answer for that. Please rephrase the question or add a bit more detail.",
    "hinglish": "Maaf kijiye, is sawal ka sahi jawab nahi mil paya. Kripya sawal thoda alag tareeke se ya detail ke saath poochhiye.",
    "hi": "क्षमा करें, इस प्रश्न का सही उत्तर नहीं मिल सका। कृपया प्रश्न को दूसरे तरीके से या थोड़ी और जानकारी के साथ पूछें।",
}


def _fail(state: GraphState, err: str, retryable: bool, t0: float, status: str) -> dict:
    latency = int((time.perf_counter() - t0) * 1000)
    memory.audit(state.get("thread_id", ""), state.get("user_id", ""), state.get("role", ""),
                 state.get("raw_question", ""), state.get("sql", ""), status, err, 0, latency)
    log.warning("execute_sql %s | %s", status, err[:300])
    can_retry = retryable and state.get("attempts", 0) < settings.max_sql_retries
    out = {"exec_error": err, "retryable": can_retry, "fatal": not can_retry, "rows": [], "columns": [], "row_count": 0}
    if not can_retry:
        msg = _FATAL_MSG.get(state.get("lang", "en"), _FATAL_MSG["en"])
        out.update({"answer": msg, "summary": msg})
    return out


def execute_sql(state: GraphState) -> dict:
    t0 = time.perf_counter()
    kb = get_kb()
    g = sql_guard.validate(state.get("sql", ""), kb, role=state.get("role", "admin"),
                           user_id=state.get("user_id", ""), max_rows=settings.max_rows,
                           member_ids=state.get("member_ids"))
    if not g.ok:
        return _fail(state, g.error, True, t0, "rejected")
    try:
        from ..db import run_select
        res = run_select(g.sql, settings.max_rows)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return _fail(state, msg, any(k in msg.lower() for k in _RETRYABLE), t0, "db_error")

    n = len(res["rows"])
    latency = int((time.perf_counter() - t0) * 1000)
    memory.audit(state.get("thread_id", ""), state.get("user_id", ""), state.get("role", ""),
                 state.get("raw_question", ""), g.sql, "ok", "", n, latency)
    zero_retry = (n == 0 and "LIKE" in g.sql.upper() and not state.get("zero_retry_done")
                  and state.get("attempts", 0) < settings.max_sql_retries)
    log.info("execute_sql ok | rows=%d truncated=%s %dms", n, res["truncated"], latency)
    return {"sql": g.sql, "columns": res["columns"], "rows": res["rows"], "row_count": n, "truncated": res["truncated"],
            "exec_error": "", "fatal": False, "zero_retry_pending": zero_retry,
            "timings": {**state.get("timings", {}), "execute_sql": round(latency / 1000, 3)}}


def route_after_execute(state: GraphState) -> str:
    if state.get("fatal"):
        return "END"
    if state.get("exec_error") and state.get("retryable"):
        return "retry_generate"
    if state.get("zero_retry_pending"):
        return "retry_generate"
    return "generate_chart"
