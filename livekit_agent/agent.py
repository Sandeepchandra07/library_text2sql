from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple

import httpx
from dotenv import load_dotenv

# LiveKit CLI (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET) aur plugins (GROQ_API_KEY)
# values os.environ se padhte hain, isliye .env yahin load karna zaroori hai.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv()  # fallback: cwd / parent folders

from livekit import agents, rtc  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    function_tool,
    room_io,
    TurnHandlingOptions,
    RunContext,
)
from livekit.plugins import groq  # noqa: E402

from .config import settings  # noqa: E402
from .tts import EdgeTTS, preprocess_for_tts  # noqa: E402,F401

logger = logging.getLogger("soul.livekit")


def _api_headers() -> dict:
    """Auth header for the text-to-SQL backend."""
    return {"X-API-Key": settings.library_agent_api_key} if settings.library_agent_api_key else {}

_LOGS_ENABLED = settings.agent_logs
if not _LOGS_ENABLED:
    logger.setLevel(logging.WARNING)


@dataclass
class SessionData:
    thread_id: str = ""
    mem_cd: str = ""
    user_name: str = ""
    # question -> (last_fail_timestamp, fail_count) — loop guard state
    recent_failed_q: Dict[str, Tuple[float, int]] = field(default_factory=dict)


_session_started_at: float = 0.0


def _log(msg: str, **extra) -> None:
    if not _LOGS_ENABLED:
        return
    if extra:
        logger.info("%s %s", msg, json.dumps(extra, ensure_ascii=False, default=str))
    else:
        logger.info(msg)


# ═══════════════════════════════════════════════════════════════
# STOP-WORD-ONLY BARGE-IN
# ═══════════════════════════════════════════════════════════════
# Speech-to-text ab browser (SpeechRecognition) mein hota hai. Agent ko
# audio nahi milta, isliye VAD se automatic interruption hoti hi nahi.
# Agent jab bol raha ho to koi bhi normal baat IGNORE hoti hai; sirf
# stop word (ruko / stop / chup ...) se agent ruk jaata hai. Uske baad
# user apna agla sawal bol sakta hai.
#
# Agent BOL RAHA HO: text mein KAHIN BHI (poore word ke roop mein) stop word
# mile to agent ruk jaata hai, aur stop word ke BAAD jo naya sawal bacha ho
# use aage answer kiya jaata hai.
# Agent CHUP HO: sirf chhota phrase (<= _STOP_MAX_WORDS words) stop maana jaata hai.
_STOP_WORDS = {
    # Roman
    "ruko", "ruk jao", "ruk jaiye", "ruk jayiye", "ruk ja",
    "chup", "chup ho jao", "chup karo",
    "stop", "stop karo", "stop kar", "wait",
    "band karo", "band kar", "bas", "bas karo",
    # Devanagari (browser hi-IN mein yahi deta hai)
    "रुको", "रुक जाओ", "रुक जाइए", "रुक जा",
    "चुप", "चुप हो जाओ", "चुप करो",
    "स्टॉप", "स्टॉप करो", "वेट",
    "बंद करो", "बंद कर", "बस", "बस करो",
}
_STOP_MAX_WORDS = 4  # agent chup ho tab: sirf itne chhote phrase (<= 4 words) ko stop maano


_WEAK_STOP = {"bas", "बस", "wait", "वेट"}   # normal sentences mein bhi aate hain
_WORD = r"[\w\u0900-\u097F]"                # \b Devanagari matras par kaam nahi karta


def _build_stop_re(words):
    alt = "|".join(sorted(map(re.escape, words), key=len, reverse=True))
    return re.compile(rf"(?<!{_WORD})({alt})(?!{_WORD})", re.IGNORECASE)


_STOP_ANY_RE = _build_stop_re(_STOP_WORDS - _WEAK_STOP)
_STOP_ALL_RE = _build_stop_re(_STOP_WORDS)


def _norm(text: str) -> str:
    t = re.sub(r"[\.,?!।;:]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _find_stop(text: str, speaking: bool):
    """None = stop nahi hai. Warna stop-word ke BAAD bacha hua text (khali ho sakta hai)."""
    t = _norm(text)
    if not t:
        return None
    if speaking:                                  # agent bol raha ho: kahin bhi stop word chalega
        ms = list(_STOP_ANY_RE.finditer(t))
        if ms:
            return t[ms[-1].end():].strip()
    if len(t.split()) <= _STOP_MAX_WORDS and _STOP_ALL_RE.search(t):
        return ""                                 # agent chup ho tab sirf chhota phrase
    return None


def _is_stop_command(text: str) -> bool:
    return _find_stop(text, False) is not None


# ═══════════════════════════════════════════════════════════════
# MEMBER NAME FETCH (For Personalized Greeting)
# ═══════════════════════════════════════════════════════════════

async def _fetch_member_name(mem_cd: str) -> str:
    """Library backend se member ka display name fetch karo."""
    if not mem_cd:
        return ""
    try:
        url = f"{settings.library_agent_url}/api/member/{mem_cd}/name"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_api_headers())
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") and data.get("name"):
                return str(data["name"]).strip()
    except Exception as e:
        logger.warning("Failed to fetch member name for %s: %s", mem_cd, e)
    return ""


# ═══════════════════════════════════════════════════════════════
# LAST QUESTION FETCH
# ═══════════════════════════════════════════════════════════════

async def _fetch_last_question(mem_cd: str) -> dict:
    """Member ka SABSE RECENT poocha gaya sawaal fetch karo (limit=1)."""
    if not mem_cd:
        return {}
    try:
        url = f"{settings.library_agent_url}/history/{mem_cd}"
        params = {"limit": 1, "offset": 0}
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params=params, headers=_api_headers())
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                entry = data[0]
                question = (entry.get("question") or "").strip()
                if question:
                    return {
                        "question": question,
                        "chat_date": entry.get("chat_date"),
                        "chat_time": entry.get("chat_time"),
                    }
    except Exception as e:
        logger.warning("Failed to fetch last question for %s: %s", mem_cd, e)
    return {}


# ═══════════════════════════════════════════════════════════════
# LOGIN SUMMARY FETCH (pre-formatted Hindi phrases — NO raw datetime)
# ═══════════════════════════════════════════════════════════════

async def _fetch_login_summary(mem_cd: str) -> dict:
    """Backend ka /login_summary endpoint: already spoken Hindi phrases deta hai."""
    if not mem_cd:
        return {}
    try:
        url = f"{settings.library_agent_url}/api/member/{mem_cd}/login_summary"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_api_headers())
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                return data
    except Exception as e:
        logger.warning("Failed to fetch login_summary for %s: %s", mem_cd, e)
    return {}


# ═══════════════════════════════════════════════════════════════
# MARKDOWN → SPEECH (Tool-layer preprocessing)
# ═══════════════════════════════════════════════════════════════

def _table_to_natural_language(answer: str) -> str:
    """Convert markdown tables in the /ask API response to spoken sentences."""
    lines = answer.split('\n')
    result = []
    in_table = False
    rows = []
    headers = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|'):
            in_table = True
            if re.match(r'^\s*\|[\s\-:|]+\|\s*$', stripped):
                continue
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if not headers:
                headers = cells
            else:
                rows.append(cells)
        else:
            if in_table and rows:
                count = len(rows)
                if count == 1:
                    details = ", ".join(f"{h} {v}" for h, v in zip(headers, rows[0]) if h and v)
                    result.append(f"Ek record mila: {details}.")
                else:
                    result.append(f"Total {count} records mile.")
                    for i, row in enumerate(rows[:2], 1):
                        details = ", ".join(f"{h} {v}" for h, v in zip(headers, row) if h and v)
                        result.append(f"Number {i}: {details}.")
                    if count > 2:
                        result.append(f"Aur {count - 2} records hain.")
                rows = []
                headers = []
                in_table = False
            result.append(line)

    if in_table and rows:
        count = len(rows)
        result.append(f"Total {count} records mile.")

    return '\n'.join(result)


def _clean_library_answer(raw_answer: str) -> str:
    """Convert /ask API markdown answer to voice-friendly plain text."""
    if not raw_answer or not raw_answer.strip():
        return "Koi record nahi mila."

    text = re.sub(r'```[\s\S]*?```', '', raw_answer)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = _table_to_natural_language(text)

    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    text = text.replace('₹', 'Rupees ')
    text = text.replace('Rs.', 'Rupees ')

    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    max_chars = settings.library_agent_max_answer_chars
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(' ', 1)[0] + ". Baaki details aapke screen par hain."

    return text


# ═══════════════════════════════════════════════════════════════
# TOOLS
# ═══════════════════════════════════════════════════════════════

_LOOP_GUARD_WINDOW_SECONDS = 30  # isi window mein same question dobara fail -> stop


@function_tool
async def query_library(question: str, context: RunContext[SessionData]) -> str:
    """Query the SOUL 3.0 library database and return a voice-friendly answer.

    Use this for ANY question about: books, members, issues, returns,
    overdue books, fines, departments, or library reports.

    Args:
        question: The user's EXACT spoken words (Hindi/Hinglish/English).
                  DO NOT translate it to English. DO NOT summarize.
                  Pass the exact transcript as it is.
    """
    ud = context.userdata
    now = time.time()
    key = question.strip()
    prev = ud.recent_failed_q.get(key)

    # ---- LOOP GUARD: same question failed abhi-abhi -> DB ko dobara mat maaro ----
    if prev and (now - prev[0]) < _LOOP_GUARD_WINDOW_SECONDS and prev[1] >= 1:
        logger.warning("query_library LOOP GUARD triggered for: %r", question)
        return (
            "[STOP_RETRY] Yeh sawal is baatcheet mein abhi hi ek baar fail ho chuka hai. "
            "Tool ko DOBARA mat bulao. User ko apne shabdon mein politely batao ki abhi "
            "yeh jawab nahi mil paa raha, aur unse alag tarike se poochne ko kaho."
        )

    url = f"{settings.library_agent_url}/ask"
    thread = ud.thread_id or "voice-session"
    user_mem_cd = ud.mem_cd

    if not user_mem_cd and not settings.voice_allow_anonymous:
        return ("Member ID nahi mili, isliye data nahi dikha sakte. "
                "User se kaho ki login karke dobara try kare. Tool ko dobara mat bulao.")

    # new backend contract: user_id (= usercode) + role. A voice caller is always a MEMBER -> sees only own data.
    payload = {
        "question": question,
        "thread_id": thread,
        "user_id": user_mem_cd,
        "role": "member" if user_mem_cd else "admin",
    }

    _log(
        "tool query_library called",
        question=question,
        thread_id=thread,
        mem_cd=user_mem_cd,
    )

    started = time.time()

    # Progressive filler messages — user ko pata rahe ki kaam chal raha hai
    async def _filler() -> None:
        fillers = [
            (4.0, "Ek second, main check kar raha hoon."),
            (14.0, "Database se data la raha hoon, thoda waqt lagega."),
            (30.0, "Abhi bhi kaam chal raha hai, thoda aur intezaar karein."),
        ]
        for delay, msg in fillers:
            await asyncio.sleep(delay)
            try:
                context.session.say(msg, allow_interruptions=True)
            except Exception:  # noqa: BLE001
                pass

    filler = asyncio.create_task(_filler())
    # Backend mein 5+ LLM calls hoti hain (Ollama local), isliye timeout zyada rakhna padta hai
    effective_timeout = max(settings.library_agent_timeout, 180.0)
    try:
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.post(url, json=payload, headers=_api_headers())
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        filler.cancel()
        logger.error("tool query_library TIMEOUT after %.0fs", effective_timeout)
        ud.recent_failed_q[key] = (now, (prev[1] + 1) if prev else 1)
        return (
            "Library system se jawab lene mein bahut zyada time lag raha hai. "
            "User ko yeh bata do apne shabdon mein, tool ko dobara mat bulao."
        )
    except Exception as e:
        filler.cancel()
        logger.error("tool query_library FAILED: %s", e)
        ud.recent_failed_q[key] = (now, (prev[1] + 1) if prev else 1)
        return (
            "Library system se connect nahi ho paya. "
            "User ko yeh bata do apne shabdon mein, tool ko dobara mat bulao."
        )

    filler.cancel()
    # `speech` = voice-ready text built by the backend; `summary` = concise text; `answer` = full markdown
    raw_answer = data.get("speech") or data.get("summary") or data.get("answer") or ""
    debug_error = data.get("debug_error")

    if debug_error:
        _log("tool query_library error from backend", debug_error=debug_error)
        ud.recent_failed_q[key] = (now, (prev[1] + 1) if prev else 1)
        return (
            "Is sawal ka jawab lene mein backend error aaya. "
            "User ko yeh bata do apne shabdon mein, tool ko dobara mat bulao."
        )

    if not raw_answer:
        _log("tool query_library empty answer")
        ud.recent_failed_q[key] = (now, (prev[1] + 1) if prev else 1)
        return (
            "Is sawal ka koi record nahi mila. "
            "User ko yeh bata do apne shabdon mein, tool ko dobara mat bulao."
        )

    # success -> loop-guard reset for this question
    ud.recent_failed_q.pop(key, None)

    clean_answer = _clean_library_answer(raw_answer)

    latency_ms = round((time.time() - started) * 1000)
    _log(
        "tool query_library succeeded",
        latency_ms=latency_ms,
        raw_chars=len(raw_answer),
        clean_chars=len(clean_answer),
    )

    return clean_answer


# ═══════════════════════════════════════════════════════════════
# AGENT DEFINITION
# ═══════════════════════════════════════════════════════════════

class LibraryAssistant(Agent):
    def __init__(self, user_name: str = "") -> None:
        name_instruction = ""
        if user_name:
            name_instruction = (
                f"The logged-in user's name is '{user_name}'. "
                f"You may address them by their first name occasionally to keep it warm and personal. "
                f"Do NOT overuse the name. "
            )

        instructions = (
            f"You are the  Library voice assistant. "
            f"You speak in the SAME language the user uses (Hindi, English, or Hinglish). "
            f"\n\n{name_instruction}\n"
            "CRITICAL RULES:\n"
            "1. ALWAYS use the query_library tool for ANY question about books, members, issues, "
            "returns, fines, reports, OR the user's OWN personal details (like 'meri details', "
            "'mera email', 'mera phone'). The tool has access to their ID, so ALWAYS call the tool.\n"
            "2. TOOL ARGUMENT RULE: Pass the user's EXACT spoken words to the 'question' parameter. "
            "DO NOT translate it to English. DO NOT summarize. Pass the exact transcript.\n"
            "3. 🚨 TOOL RESULT RELAY RULE (MOST IMPORTANT — DO NOT VIOLATE):\n"
            "   The tool's result is ALREADY a complete, voice-ready, formatted answer in Hindi/Hinglish.\n"
            "   You MUST relay EVERY SPECIFIC VALUE it contains — names, email addresses, phone numbers, "
            "   dates, amounts, department names, book titles — EXACTLY as given.\n"
            "   🚫 NEVER replace an actual value with just the field's label.\n"
            "   ❌ WRONG: 'Aapka naam, email, phone, department mil gaya hai.' (values missing — SEVERE ERROR)\n"
            "   ✅ CORRECT: 'Aapka naam Raj hai, email raj123 at the rate gmail dot com hai, phone 9 8 7 6 5 4 3 2 1 0 hai...'\n"
            "   If unsure whether to shorten something, DO NOT shorten it — repeat the tool's text almost "
            "   word-for-word instead. Losing a value is a critical failure; being slightly long is not.\n"
            "4. Brevity applies ONLY to removing filler/repetition — NEVER to dropping actual data. "
            "If the tool result has 5 fields with values, your reply must mention all 5 values.\n"
            "5. Only for GENUINELY LONG multi-row results (5+ records) may you summarize counts and give "
            "just the first 2-3 examples. Single-record detail answers (member info, fine info, book info) "
            "must ALWAYS include every field's actual value.\n"
            "6. The tool returns CLEAN plain text — do NOT add markdown, tables, bullets, "
            "serial numbers, or code blocks in your reply.\n"
            "7. If the tool says 'no records', politely say so in user's language.\n"
            "8. NEVER mention SQL, database, files, downloads, or markdown.\n"
            "9. Speak numbers naturally: digit-by-digit for phone numbers, spelled out for emails.\n"
            "10. If user says 'thank you' or 'dhanyavaad', reply warmly and ask if they need anything else.\n"
            "11. If a tool result starts with '[STOP_RETRY]', do NOT call any tool again. "
            "Just tell the user in your own words that you couldn't get an answer this time "
            "and ask them to rephrase or try again later."
        )

        # NOTE: yahan vad/stt nahi hai — user ki baat browser se text ban kar aati hai.
        super().__init__(
            instructions=instructions,
            tools=[query_library],
        )


# ═══════════════════════════════════════════════════════════════
# SERVER & SESSION
# ═══════════════════════════════════════════════════════════════

server = AgentServer()

# Browser is topic par JSON bhejta hai: {"type": "final" | "stop", "text": "...", "confidence": 0.9}
USER_TEXT_TOPIC = "user-transcript"



@server.rtc_session(agent_name=settings.agent_name)
async def library_assistant_session(ctx: agents.JobContext) -> None:
    global _session_started_at
    _session_started_at = time.time()
    job = ctx.job

    _log(
        "AGENT SPAWNED",
        event="spawned",
        job_id=job.id,
        room=ctx.room.name,
        agent_name=settings.agent_name,
    )

    # Parse dispatch metadata
    meta: dict = {}
    if job.metadata:
        try:
            meta = json.loads(job.metadata)
        except json.JSONDecodeError:
            logger.warning("Unparseable job metadata: %s", job.metadata)

    session_data = SessionData(
        thread_id=str(meta.get("thread_id") or ""),
        mem_cd=str(meta.get("user_id") or ""),
    )

    _log("AGENT METADATA", thread_id=session_data.thread_id, mem_cd=session_data.mem_cd)

    # Fetch name and last question in parallel (login summary ab use nahi hota)
    member_name, last_question_data = await asyncio.gather(
        _fetch_member_name(session_data.mem_cd),
        _fetch_last_question(session_data.mem_cd),
    )
    session_data.user_name = member_name
    _log("AGENT MEMBER NAME RESOLVED", name=member_name or "<unknown>")

    last_question = last_question_data.get("question", "")
    _log("AGENT LAST QUESTION RESOLVED", has_last_question=bool(last_question))

    # Participant lifecycle
    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        _log("PARTICIPANT CONNECTED", identity=participant.identity)

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        _log("PARTICIPANT DISCONNECTED", identity=participant.identity)

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*args) -> None:
        duration = round(time.time() - _session_started_at, 2)
        _log("AGENT SESSION ENDED", duration_s=duration)

    for identity in ctx.room.remote_participants:
        _log("PARTICIPANT ALREADY IN ROOM", identity=identity)

    # Configure LLM based on settings
    if settings.llm_provider == "ollama":
        from livekit.plugins import openai as lk_openai
        llm = lk_openai.LLM(
            model=settings.ollama_model,
            api_key="ollama",
            base_url=settings.ollama_base_url,
            temperature=0.0,
        )
        _log("LLM CONFIGURED", provider="ollama", model=settings.ollama_model)
    else:
        llm = groq.LLM(model=settings.groq_model, temperature=0.0)
        _log("LLM CONFIGURED", provider="groq", model=settings.groq_model)

    # ── SESSION: no VAD / STT / turn-detector ──────────────────
    # Turn ka faisla browser karta hai (final transcript aane par hi reply banta hai).
    session = AgentSession(
        llm=llm,
        tts=EdgeTTS(),
        userdata=session_data,
        turn_handling=TurnHandlingOptions(
            turn_detection="manual",
            preemptive_generation={"enabled": False},
        ),
    )

    # Diagnostic event logging
    session.on("agent_state_changed", lambda ev: _log(
        "AGENT STATE", old=ev.old_state, new=ev.new_state,
    ))
    session.on("function_tools_executed", lambda ev: _log(
        "TOOLS EXECUTED", calls=[c.name for c in ev.function_calls],
    ))
    session.on("error", lambda ev: logger.error(
        "SESSION ERROR %s: %s (source=%s)",
        type(ev.error).__name__, ev.error, ev.source,
    ))

    def _do_interrupt() -> None:
        """Agent ki current speech + generation turant rok do."""
        try:
            session.interrupt(force=True)
        except TypeError:
            # purane SDK mein force argument nahi hota
            session.interrupt()
        except Exception as e:
            logger.error("session.interrupt() failed: %s", e)

    def _agent_is_speaking() -> bool:
        return str(getattr(session, "agent_state", "")) == "speaking"

    # ── BROWSER TEXT -> AGENT ──────────────────────────────────
    async def _handle_browser_msg(raw: str) -> None:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                msg = {"type": "final", "text": str(msg)}
        except json.JSONDecodeError:
            msg = {"type": "final", "text": raw}

        text = str(msg.get("text") or "").strip()
        msg_type = msg.get("type", "final")
        if not text and msg_type != "stop":
            return

        _log("USER TRANSCRIBED", type=msg_type, transcript=text, conf=msg.get("confidence"))

        # 1) STOP: agent ruk jaaye. Stop word ke baad naya sawal ho to wahi answer karo.
        speaking = _agent_is_speaking()
        remainder = "" if msg_type == "stop" else _find_stop(text, speaking)
        if remainder is not None:
            _log("STOP COMMAND DETECTED", transcript=text, remainder=remainder, speaking=speaking)
            _do_interrupt()                       # async version lagaya ho to: await _do_interrupt()
            if len(remainder.split()) >= 2:       # "stop stop mera department batao" -> "mera department batao"
                session.generate_reply(user_input=remainder)
            return

        # 2) Agent bol raha hai: normal baat ignore
        if speaking:
            _log("IGNORED (agent speaking, not a stop word)", transcript=text)
            return

        # 3) Agent soch raha tha (tool/LLM chal raha): purana cancel karo, naya sawal lo
        _do_interrupt()
        session.generate_reply(user_input=text)

    def _on_text_stream(reader, participant_identity: str) -> None:
        async def _run() -> None:
            try:
                await _handle_browser_msg(await reader.read_all())
            except Exception:
                logger.exception("user-transcript handler failed")

        asyncio.create_task(_run())

    ctx.room.register_text_stream_handler(USER_TEXT_TOPIC, _on_text_stream)

    # Start session (room ka audio agent tak nahi jaata; agent ki awaaz user tak jaati hai)
    await session.start(
        room=ctx.room,
        agent=LibraryAssistant(user_name=member_name),
        room_options=room_io.RoomOptions(audio_input=False),
    )

    _log("AGENT SESSION STARTED", event="session_started")

    # ═══════════════════════════════════════════════════════════
    # GREETING — ORDER:
    # (1) Hello + Name -> (2) Last Question Asked -> (3) Open question
    # (login summary wali line hata di gayi hai)
    # ═══════════════════════════════════════════════════════════

    if member_name:
        greeting_line = f"Namaste {member_name} ji, main  Library assistant hoon."
    else:
        greeting_line = "Namaste! Main  Library assistant hoon."

    last_question_line = ""
    if last_question:
        last_question_line = f"Waise, pichli baar aapne yeh poocha tha: {last_question}"

    closing_line = "Aap aaj library ke baare mein kya jaanna chahte hain?"

    parts = [greeting_line]
    if last_question_line:
        parts.append(last_question_line)
    parts.append(closing_line)

    final_greeting_text = " ".join(parts)

    _log(
        "AGENT GREETING TEXT BUILT",
        greeting_chars=len(final_greeting_text),
        has_last_question_line=bool(last_question_line),
    )

    # Deterministic speech; greeting ko bhi "stop" bolke roka ja sakta hai.
    say_fn = getattr(session, "say", None)
    if callable(say_fn):
        try:
            await say_fn(final_greeting_text, allow_interruptions=True)
        except TypeError:
            await say_fn(final_greeting_text)
    else:
        numbered_parts = "\n".join(f'PART {i + 1}: "{p}"' for i, p in enumerate(parts))
        greeting_instructions = (
            f"Neeche diye gaye {len(parts)} parts ko EXACT ISI ORDER mein, "
            f"BILKUL WAISE HI (bina translate/paraphrase kiye) bolo:\n\n"
            f"{numbered_parts}\n\n"
            "RULES:\n"
            "- Sabhi parts ko ek dusre ke baad bolo, order mat badlo.\n"
            "- In lines ko bilkul waisi hi bolo jaisi di gayi hain — inhe dobara likho mat, "
            "translate mat karo, sirf naturally spoken tone mein deliver karo.\n"
            "- Do NOT call any tools. Do NOT add extra content beyond these parts."
        )
        await session.generate_reply(instructions=greeting_instructions)

    _log("AGENT GREETING COMPLETE", latency_s=round(time.time() - _session_started_at, 2))


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT  (yahi missing tha — iske bina process bina kuch kiye exit ho jata tha)
# Run:  python -m livekit_agent.agent dev     (development)
#       python -m livekit_agent.agent start   (production)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    agents.cli.run_app(server)