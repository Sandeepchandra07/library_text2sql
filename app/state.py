from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    # ---- input
    thread_id: str
    user_id: str
    role: str                     # "admin" | "member"
    member_ids: list[str]         # verified identifiers of the caller (usercode, userid)
    member_code: str
    member_col: str               # 'usercode' or 'userid' (which vMember column holds member_code)
    member_key: str               # value used for circulation tables' userid
    member_name: str
    raw_question: str
    # ---- preprocessing
    lang: str                     # en | hinglish | hi
    corrected_question: str
    history: list[dict[str, Any]]
    is_followup: bool
    standalone_question: str
    # ---- understanding
    understanding: dict[str, Any]
    route: str                    # "sql" | "direct"
    tables: list[str]
    schema_context: str
    rules_context: str
    # ---- SQL loop
    sql: str
    sql_template: str             # verified KPI SQL (app/metrics.py) - bypasses the LLM
    attempts: int
    exec_error: str
    retryable: bool
    zero_retry_pending: bool
    zero_retry_done: bool
    fatal: bool
    # ---- result
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    chart: dict[str, Any] | None
    summary: str
    answer: str
    timings: dict[str, float]
