"""Node 1: correct_asr_errors   Node 2: resolve_followup"""
from __future__ import annotations

import logging
import re
import time

from rapidfuzz import fuzz, process

from .. import domain, llm, memory
from ..state import GraphState

log = logging.getLogger(__name__)

_VOCAB = [
    "accession", "catalog", "circulation", "overdue", "publisher", "author", "authors", "fine", "member", "members",
    "department", "vendor", "budget", "arrival", "edition", "issued", "returned", "available", "pending", "library",
    "subject", "student", "faculty", "digital", "isbn", "title", "copies", "membership", "expired", "borrowed",
    "language", "program", "location", "purchase", "order", "session", "yesterday", "tomorrow", "month", "week",
]
_VOCAB_SET = set(_VOCAB)


def _fuzzy_fix(text: str) -> tuple[str, bool]:
    """Fix near-miss spellings of DOMAIN words only (accesion -> accession). Names are left alone."""
    changed = False

    def fix(m: re.Match) -> str:
        nonlocal changed
        w = m.group(0)
        lw = w.lower()
        if len(lw) < 6 or lw in _VOCAB_SET or w[0].isupper():
            return w
        hit = process.extractOne(lw, _VOCAB, scorer=fuzz.ratio, score_cutoff=88)
        if hit:
            changed = True
            return hit[0]
        return w

    return re.sub(r"[A-Za-z]+", fix, text), changed


_ASR_SYS = """You clean up library-assistant questions that may come from speech recognition (ASR) or be written in
Hindi / Hinglish. Rewrite the question as ONE clear English question for a library database.
Rules:
- Keep the exact meaning, numbers, dates and every filter. Do NOT answer the question. Do NOT add information.
- Convert Hindi (Devanagari) or Hinglish to English. Transliterate names of people, books, authors, publishers
  into Latin letters as they would appear in a library catalogue (e.g. "चेतन भगत" -> "Chetan Bhagat").
- Fix obvious ASR mistakes using library vocabulary (accession, issue, overdue, fine, publisher, ISBN, catalog...).
- Common Hindi words: kitab=book, lekhak=author, prakashak=publisher, sadasya=member, jurmana=fine, wapas=return,
  kitni/kitne=how many, dikhao/batao=show, sabhi=all, aaj=today, is mahine=this month, pichle=last, naya=new.
Return JSON: {"corrected_question": "..."}"""


def correct_asr_errors(state: GraphState) -> dict:
    t0 = time.perf_counter()
    raw = (state.get("raw_question") or "").strip()
    lang = domain.detect_language(raw)
    fixed, changed = _fuzzy_fix(raw)
    corrected = fixed
    if lang != "en" or changed or re.search(r"[^\x00-\x7F]", raw):
        out = llm.chat_json(_ASR_SYS, f"Question: {fixed}")
        cand = str(out.get("corrected_question", "")).strip()
        if cand and len(cand) < 3 * max(len(fixed), 20):
            corrected = cand
    log.info("asr | lang=%s | %r -> %r", lang, raw, corrected)
    return {"lang": lang, "corrected_question": corrected,
            "timings": {**state.get("timings", {}), "asr": round(time.perf_counter() - t0, 3)}}


# ------------------------------------------------------------------------------------------ follow-up
_FOLLOW = re.compile(
    r"\b(it|its|that|those|them|this|these|his|her|their|he|she|same|above|previous|earlier|too|also|again|instead|"
    r"uska|uski|unka|unki|isme|usme|inme|wahi|aur|ab|phir|bhi)\b|^\s*(and|what about|how about|only|now|but)\b", re.I)

_FU_SYS = """You rewrite a follow-up question into a fully self-contained question for a library database assistant.
You get the recent conversation and the NEW question.
- If the new question depends on earlier turns (pronouns like it/them/his, or omitted subject/filters such as
  "and for last month?", "only overdue ones", "their emails"), merge the needed context (entity names, time range,
  filters) into one standalone English question.
- If the new question is already self-contained, return it unchanged and is_followup=false.
- Never invent details that are not in the conversation. Keep names/titles exactly as they appear.
- NEVER add personal attributes of the user (program, department, role, status, id) unless the NEW question itself says them.
- Add ONLY what is required to make the question understandable (missing entity name, time range or filter). "my phone number" is already self-contained.
Return JSON: {"is_followup": true|false, "standalone_question": "..."}"""


def resolve_followup(state: GraphState) -> dict:
    t0 = time.perf_counter()
    q = state.get("corrected_question") or state.get("raw_question", "")
    history = memory.recent(state.get("thread_id", "default"), 4)
    result = {"history": history, "is_followup": False, "standalone_question": q}
    if history and (len(q.split()) <= 4 or _FOLLOW.search(q)):
        convo = "\n".join(f"User: {h['standalone'] or h['question']}\nAssistant: {h['summary']}" for h in history)
        out = llm.chat_json(_FU_SYS, f"Conversation:\n{convo}\n\nNEW question: {q}")
        cand = str(out.get("standalone_question", "")).strip()
        if cand and out.get("is_followup") is True:      # keep the original wording unless it truly is a follow-up
            result["standalone_question"] = cand
            result["is_followup"] = True
    log.info("followup | %r -> %r (followup=%s)", q, result["standalone_question"], result["is_followup"])
    result["timings"] = {**state.get("timings", {}), "followup": round(time.perf_counter() - t0, 3)}
    return result
