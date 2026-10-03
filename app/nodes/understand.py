"""Node 3: question_understanding - intent, table selection, business rules, schema context."""
from __future__ import annotations

import logging
import time

from .. import domain, llm, metrics
from ..runtime import get_kb
from ..state import GraphState

log = logging.getLogger(__name__)

_SYS = """You are the analyst of a library-management chatbot (database of books, copies, members, issue/return,
fines, publishers, vendors, budgets, orders, digital content). Analyse the user's question.

Return JSON with these keys:
- "intent": "data_query" (needs database) | "greeting" | "out_of_scope" (not about the library data) | "needs_clarification"
- "tables": up to 5 table names that are needed, chosen ONLY from CANDIDATE TABLES
- "operation": one of "count","list","detail","top_n","aggregate","trend","exists","other"
- "filters": object with any of title, author, publisher, member_name, member_id, department, subject, date_range, status, number
- "wants_chart": true only if the user explicitly asks for chart/graph/plot/trend/comparison visual
- "assumptions": short list of interpretations you made (e.g. "book = title and copies")
- "clarification": ONE short question, only if intent is needs_clarification (a missing detail that changes the answer)
- "direct_reply": short friendly reply, only for greeting/out_of_scope, in the user's language
Rules: prefer data_query whenever the library database could answer. Ask for clarification ONLY when essential
(e.g. "show details" with no subject at all). Reasonable defaults beat questions."""


def question_understanding(state: GraphState) -> dict:
    t0 = time.perf_counter()
    kb = get_kb()
    q = state["standalone_question"]
    role = state.get("role", "admin")

    if role == "member":  # "my phone / my profile / my fine ..." -> deterministic, no LLM needed
        fields = domain.self_profile_fields(q)
        if fields and kb.has_table("vmember"):
            log.info("understand | self-profile fast path fields=%s", fields)
            return {"understanding": {"intent": "data_query", "operation": "detail", "self_fields": fields},
                    "route": "sql", "tables": ["vmember"], "schema_context": kb.render_schema(q, ["vmember"]),
                    "rules_context": "", "attempts": 0, "zero_retry_done": True,
                    "timings": {**state.get("timings", {}), "understand": round(time.perf_counter() - t0, 3)}}

    met = metrics.match(q, role)
    if met and all(kb.has_table(t) for t in met.tables):   # verified template, no LLM involved
        log.info("understand | KPI template %s", met.name)
        return {"understanding": {"intent": "data_query", "operation": "count", "template": met.name}, "route": "sql",
                "tables": list(met.tables), "schema_context": kb.render_schema(q, list(met.tables)), "rules_context": "",
                "sql_template": met.sql, "attempts": 0, "zero_retry_done": True,
                "timings": {**state.get("timings", {}), "understand": round(time.perf_counter() - t0, 3)}}

    ranked = [(t, s) for t, s in kb.score_tables(q) if s > 0][:9]
    cand = [t for t, _ in ranked] or ["bookcatalog", "bookaccessionmaster", "circusermanagement", "circissuetransaction"]
    cand_txt = "\n".join(f"- {kb.tables[t].name}: {domain.TABLE_NOTES.get(t, '')}" for t in cand)
    role_note = ""
    if role == "member":
        lang_name = {"en": "English", "hinglish": "Hinglish (Hindi in Latin letters)", "hi": "Hindi (Devanagari)"}.get(
            state.get("lang", "en"), "English")
        role_note = (
            f"\nThe caller is a library MEMBER named '{state.get('member_name', '')}'. They may see ONLY their own data "
            f"(profile, fine, membership validity, own issued/returned books) plus general catalogue information "
            f"(books, authors, availability). 'my / mera / meri / mujhe / I' = the caller.\n"
            f"If the question asks for personal data of ANY OTHER person (another member's profile, fine, phone, issued books, "
            f"or a list/count of members), set intent=\"out_of_scope\" and direct_reply = a polite one-line refusal saying "
            f"you can only share the member's own information, written in {lang_name}.")

    out = llm.chat_json(_SYS + role_note, f"CANDIDATE TABLES:\n{cand_txt}\n\nQuestion: {q}", max_tokens=600)
    intent = out.get("intent", "data_query")
    if intent not in {"data_query", "greeting", "out_of_scope", "needs_clarification"}:
        intent = "data_query"
    out["intent"] = intent

    if intent != "data_query":
        reply = out.get("clarification") if intent == "needs_clarification" else out.get("direct_reply")
        reply = reply or {
            "greeting": "Hello! I can answer questions about books, members, issues, returns, fines and more.",
            "out_of_scope": "I can only answer questions about the library database (books, members, circulation, fines, orders...).",
            "needs_clarification": "Could you give a little more detail so I can find the right data?",
        }[intent]
        return {"understanding": out, "route": "direct", "answer": reply, "summary": reply, "rows": [], "columns": [],
                "row_count": 0, "tables": [], "sql": "",
                "timings": {**state.get("timings", {}), "understand": round(time.perf_counter() - t0, 3)}}

    llm_tables = [t for t in (out.get("tables") or []) if isinstance(t, str) and kb.has_table(t)]
    tables = kb.select_tables(q, extra_tables=llm_tables, k=4, cap=7)
    if role == "member":
        tables = [t for t in tables if t in domain.MEMBER_ALLOWED_TABLES] or ["bookcatalog", "bookaccessionmaster"]
    rules = domain.match_rules(q)
    schema_ctx = kb.render_schema(q, tables)
    joins = kb.join_hints(tables)
    if joins:
        schema_ctx += "\n\nJOIN HINTS:\n" + "\n".join(f"  {j}" for j in joins)
    log.info("understand | intent=%s op=%s tables=%s rules=%d", intent, out.get("operation"), tables, len(rules))
    return {"understanding": out, "route": "sql", "tables": tables, "schema_context": schema_ctx,
            "rules_context": "\n".join(rules), "attempts": 0, "zero_retry_done": False,
            "timings": {**state.get("timings", {}), "understand": round(time.perf_counter() - t0, 3)}}
