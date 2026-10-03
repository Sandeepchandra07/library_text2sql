"""Nodes 7-9: generate_chart -> generate_answer -> update_memory"""
from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import os
import re
import time
from typing import Any

from .. import llm, memory
from ..state import GraphState

log = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------------- chart
def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numeric_cols(columns: list[str], rows: list[list[Any]]) -> list[int]:
    idx = []
    for i in range(len(columns)):
        vals = [r[i] for r in rows if r[i] is not None]
        if vals and all(_is_num(v) for v in vals):
            idx.append(i)
    return idx


def build_chart(columns: list[str], rows: list[list[Any]], wants_chart: bool, question: str) -> dict | None:
    if len(rows) < 2 or len(columns) < 2:
        return None
    num = _numeric_cols(columns, rows)
    txt = [i for i in range(len(columns)) if i not in num]
    if not num or len(txt) != 1 and not (wants_chart and txt):
        return None
    if len(rows) > 30 and not wants_chart:
        return None
    x = txt[0]
    # ID-like numeric columns are not metrics
    metrics = [i for i in num if not re.search(r"(^|_)(id|no|number|code)$", columns[i], re.I)] or num
    labels = [str(r[x]) for r in rows]
    is_time = all(re.match(r"^\d{4}([-/]\d{2})?", l) for l in labels)
    q = question.lower()
    ctype = "line" if (is_time or "trend" in q) else "pie" if ("pie" in q and len(rows) <= 8) else "bar"
    if not wants_chart and not is_time and len(txt) == 1 and len(rows) < 3:
        return None
    return {
        "type": ctype, "title": f"{', '.join(columns[i] for i in metrics)} by {columns[x]}".replace("_", " "),
        "x": columns[x], "labels": labels,
        "series": [{"name": columns[i], "values": [r[i] for r in rows]} for i in metrics[:3]],
    }


def _png(chart: dict) -> str | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for s in chart["series"]:
            vals = [v or 0 for v in s["values"]]
            if chart["type"] == "line":
                ax.plot(chart["labels"], vals, marker="o", label=s["name"])
            elif chart["type"] == "pie":
                ax.pie(vals, labels=chart["labels"], autopct="%1.0f%%")
                break
            else:
                ax.bar(chart["labels"], vals, label=s["name"])
        ax.set_title(chart["title"])
        if chart["type"] != "pie":
            plt.xticks(rotation=45, ha="right")
            if len(chart["series"]) > 1:
                ax.legend()
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # noqa: BLE001
        log.warning("png render failed: %s", e)
        return None


def generate_chart(state: GraphState) -> dict:
    t0 = time.perf_counter()
    u = state.get("understanding", {})
    chart = build_chart(state.get("columns", []), state.get("rows", []), bool(u.get("wants_chart")),
                        state.get("standalone_question", ""))
    if chart and os.getenv("CHART_PNG", "0") == "1":
        chart["png_base64"] = _png(chart)
    return {"chart": chart, "timings": {**state.get("timings", {}), "chart": round(time.perf_counter() - t0, 3)}}


# ------------------------------------------------------------------------------------------ answer
def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    s = str(v)
    if re.match(r"^\d{4}-\d{2}-\d{2}T00:00:00$", s):
        s = s[:10]
    s = s.replace("T", " ") if re.match(r"^\d{4}-\d{2}-\d{2}T", s) else s
    return s.replace("|", "/").replace("\n", " ")[:80]


def markdown_table(columns: list[str], rows: list[list[Any]], limit: int = 25) -> str:
    head = "| " + " | ".join(c.replace("_", " ").title() for c in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(_fmt(v) for v in r) + " |" for r in rows[:limit]]
    return "\n".join([head, sep, *body])


_LANG = {"en": "English", "hinglish": "Hinglish (Hindi written in Roman/Latin letters)",
         "hi": "Hindi in Devanagari script"}
_NONE = {
    "en": "No matching records were found.",
    "hinglish": "Koi matching record nahi mila.",
    "hi": "कोई मिलता-जुलता रिकॉर्ड नहीं मिला।",
}
_SYS = """You are a friendly library assistant. Write a SHORT answer (1-3 sentences) to the user's question using ONLY the
DATA given. Reply in {lang}. Keep book titles, names and codes exactly as in the data.
- State exact numbers from the data; never estimate, never invent facts, never mention SQL or tables.
- If the data has many rows, give the headline (count, top item, total) - a table is shown separately, do not repeat it.
- If 'truncated' is true, say only the first rows are shown."""


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def _numbers_ok(answer: str, data_text: str, extra: set[str]) -> bool:
    allowed = _numbers(data_text) | extra
    for n in _numbers(answer):
        n2 = n.rstrip(".")
        if n2 in allowed or (n2.isdigit() and int(n2) <= 10) or re.match(r"^(19|20)\d\d$", n2):
            continue
        try:
            if any(abs(float(n2) - float(a)) < 0.01 for a in allowed if re.match(r"^\d+\.?\d*$", a)):
                continue
        except ValueError:
            pass
        return False
    return True


def _fallback_summary(state: GraphState) -> str:
    cols, rows, n = state.get("columns", []), state.get("rows", []), state.get("row_count", 0)
    lang = state.get("lang", "en")
    # For member profile (many columns, 1 row) → natural Hindi/Hinglish
    if n == 1 and len(cols) > 2:
        parts = []
        row = rows[0]
        for c, v in zip(cols, row):
            vstr = _fmt(v)
            if vstr in ("-", ""):
                continue
            cname = c.lower()
            # Map common names
            if cname in ("name", "member_name"):
                parts.append(f"naam {vstr}")
            elif cname in ("usercode", "member_code"):
                parts.append(f"member code {vstr}")
            elif cname in ("email1", "email"):
                parts.append(f"email {vstr}")
            elif cname in ("phone1", "phone", "phone_number"):
                parts.append(f"phone {vstr}")
            elif cname in ("departmentname", "department"):
                parts.append(f"department {vstr}")
            elif cname in ("program_name", "program"):
                parts.append(f"program {vstr}")
            elif cname in ("classname", "member_group"):
                parts.append(f"class {vstr}")
            elif cname in ("session"):
                parts.append(f"session {vstr}")
            elif cname in ("validupto", "valid_upto"):
                parts.append(f"valid upto {vstr}")
            elif cname in ("status", "membership_status"):
                parts.append(f"status {vstr}")
            elif cname in ("finebal", "fine_balance"):
                parts.append(f"fine balance {vstr}")
            elif cname in ("issd", "books_currently_issued"):
                parts.append(f"books issued {vstr}")
            else:
                label = c.replace('_', ' ')
                # clean
                if label.endswith('1'): label = label[:-1]
                parts.append(f"{label} {vstr}")
        if parts:
            # Natural phrasing Hindi/Hinglish
            if lang in ("hi", "hinglish"):
                return "Aapki details yeh hain: " + ", ".join(parts) + "."
            return "Your profile details: " + ", ".join(parts) + "."
    if n == 1 and len(cols) <= 4:
        # keep simple
        return "; ".join(f"{c.replace('_', ' ')}: {_fmt(v)}" for c, v in zip(cols, rows[0]))
    # Multi-row: include first 2 records' data for a richer voice answer
    if n > 1 and len(cols) >= 1:
        # Pick meaningful columns (skip IDs/codes that are just numbers)
        show_cols = [(i, c) for i, c in enumerate(cols) if not re.match(r"^(id|inst_?id|ctrl_?no)$", c.lower())][:4]
        if not show_cols:
            show_cols = list(enumerate(cols))[:3]
        preview_rows = rows[:2]
        previews = []
        for idx, row in enumerate(preview_rows, 1):
            parts = [f"{c.replace('_', ' ')} {_fmt(row[i])}" for i, c in show_cols if row[i] is not None and _fmt(row[i]) != "-"]
            if parts:
                previews.append(f"Number {idx}: " + ", ".join(parts))
        if lang in ("hi", "hinglish"):
            result = f"Total {n} records mile hain."
            if previews:
                result += " " + ". ".join(previews) + "."
                if n > 2:
                    result += f" Aur {n - 2} records hain."
            return result
        result = f"{n} record(s) found."
        if previews:
            result += " " + ". ".join(previews) + "."
            if n > 2:
                result += f" And {n - 2} more."
        return result
    if lang in ("hi", "hinglish"):
        return f"Total {n} record mile hain."
    return f"{n} record(s) found."


def generate_answer(state: GraphState) -> dict:
    t0 = time.perf_counter()
    lang = state.get("lang", "en")
    cols, rows, n = state.get("columns", []), state.get("rows", []), state.get("row_count", 0)
    if n == 0:
        summary = _NONE.get(lang, _NONE["en"])
        return {"summary": summary, "answer": summary}

    shown = rows[:30]
    data_text = "columns: " + ", ".join(cols) + "\n" + "\n".join(" | ".join(_fmt(v) for v in r) for r in shown)
    user = (f"Question: {state.get('standalone_question')}\nrow_count: {n}\ntruncated: {state.get('truncated', False)}\n"
            f"DATA (first {len(shown)} rows):\n{data_text}")
    system = _SYS.format(lang=_LANG.get(lang, "English"))

    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except Exception:  # noqa: BLE001
        writer = None
    parts: list[str] = []
    try:
        for tok in llm.chat_stream(system, user):
            parts.append(tok)
            if writer:
                writer({"token": tok})
        summary = "".join(parts).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("answer LLM failed: %s", e)
        summary = ""
    if not summary or not _numbers_ok(summary, data_text, {str(n), str(len(shown)), str(len(rows))}):
        if summary:
            log.warning("numeric guard rejected LLM summary: %r", summary)
        summary = _fallback_summary(state)

    answer = summary
    if n == 1 and len(cols) > 3:      # e.g. profile: one row, many columns -> vertical list
        answer += "\n\n" + "\n".join(f"- **{c.replace('_', ' ').title()}:** {_fmt(v)}" for c, v in zip(cols, rows[0]))
    elif not (n == 1 and len(cols) == 1):
        answer += "\n\n" + markdown_table(cols, rows)
        if n > 25:
            answer += f"\n\n_Showing 25 of {n}{'+' if state.get('truncated') else ''} rows._"
    return {"summary": summary, "answer": answer,
            "timings": {**state.get("timings", {}), "answer": round(time.perf_counter() - t0, 3)}}


# ------------------------------------------------------------------------------------------ memory
def update_memory(state: GraphState) -> dict:
    try:
        memory.add_turn(state.get("thread_id", "default"), state.get("user_id", ""), state.get("raw_question", ""),
                        state.get("standalone_question", ""), state.get("sql", ""), state.get("summary", ""),
                        state.get("row_count", 0), state.get("tables", []))
    except Exception as e:  # noqa: BLE001
        log.error("memory write failed: %s", e)
    return {}
