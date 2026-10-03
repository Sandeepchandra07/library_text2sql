from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import memory
from .config import settings
from .graph import get_graph, initial_state
from .runtime import get_kb, reload_kb

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=lambda: (get_kb(), get_graph()), daemon=True).start()  # warm up
    yield


app = FastAPI(title="Library Text-to-SQL (LangGraph)", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from .livekit_routes import router as livekit_router
app.include_router(livekit_router)

def check_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and not hmac.compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="invalid API key")


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=600)
    thread_id: str | None = None
    user_id: str = ""
    role: Literal["admin", "member"] = "admin"


def _prepare(req: AskRequest) -> tuple[AskRequest, dict[str, Any]]:
    """user_id given  => member mode: caller is verified against vMember and sees ONLY own data.
    user_id empty     => uses the `role` from the request (admin)."""
    req.user_id = req.user_id.strip()
    if req.user_id:
        req.role = "member"          # a user id can never be combined with admin scope
    if req.role == "member" and not req.user_id:
        raise HTTPException(status_code=400, detail="user_id is required for role=member")
    req.thread_id = req.thread_id or str(uuid.uuid4())
    state: dict[str, Any] = dict(initial_state(req.question, req.thread_id, req.user_id, req.role))
    if req.role == "member":
        from .db import resolve_member
        try:
            m = resolve_member(req.user_id)
        except Exception:  # noqa: BLE001
            log.exception("member lookup failed")
            raise HTTPException(status_code=503, detail="member lookup unavailable")
        if not m:
            raise HTTPException(status_code=404, detail="member not found")
        ids = [x for x in (m["usercode"], m["userid"]) if x]
        state.update({"member_ids": ids, "member_code": m["usercode"] or m["userid"],
                      "member_key": m["usercode"] or m["userid"], "member_name": m["name"],
                      "member_col": "usercode" if m["usercode"] else "userid"})
    return req, state


def _response(state: dict[str, Any], role: str) -> dict[str, Any]:
    summary = state.get("summary", "")
    out = {
        "thread_id": state.get("thread_id"),
        "answer": state.get("answer", ""),
        "summary": summary,          # short text: ideal for TTS / voice agent
        "speech": summary,            # voice-ready alias — agent.py reads this first
        "columns": state.get("columns", []),
        "rows": state.get("rows", []),
        "row_count": state.get("row_count", 0),
        "truncated": state.get("truncated", False),
        "chart": state.get("chart"),
        "standalone_question": state.get("standalone_question"),
        "lang": state.get("lang"),
        "timings": state.get("timings", {}),
    }
    if role == "admin":  # never leak SQL / schema details to members
        out["sql"] = state.get("sql", "")
        out["tables"] = state.get("tables", [])
        out["attempts"] = state.get("attempts", 0)
    return out


@app.post("/ask", dependencies=[Depends(check_key)])
def ask(req: AskRequest) -> dict[str, Any]:
    req, init = _prepare(req)
    t0 = time.perf_counter()
    try:
        state = get_graph().invoke(init)
    except Exception:  # noqa: BLE001
        log.exception("graph failure")
        raise HTTPException(status_code=500, detail="internal error while answering")
    res = _response(state, req.role)
    res["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return res


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/ask/stream", dependencies=[Depends(check_key)])
def ask_stream(req: AskRequest) -> StreamingResponse:
    req, init = _prepare(req)

    def gen() -> Iterator[str]:
        state: dict[str, Any] = dict(init)
        try:
            for mode, chunk in get_graph().stream(state, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    yield _sse("token", chunk.get("token", ""))
                else:
                    for node, upd in chunk.items():
                        state.update(upd or {})
                        yield _sse("node", node)
            yield _sse("final", _response(state, req.role))
        except Exception:  # noqa: BLE001
            log.exception("stream failure")
            yield _sse("error", "internal error while answering")

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/history/{user_id}", dependencies=[Depends(check_key)])
def history(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return memory.user_history(user_id, min(limit, 100))


@app.post("/admin/reload-schema", dependencies=[Depends(check_key)])
def reload_schema() -> dict[str, Any]:
    kb = reload_kb()
    return {"tables": len(kb.tables), "edges": len(kb.edges), "profiled_columns": len(kb.value_profile)}


@app.get("/health")
def health() -> dict[str, Any]:
    from .db import ping
    kb = get_kb()
    return {"status": "ok", "db": ping(), "tables": len(kb.tables), "model": settings.ollama_model}


# ---- static frontend (must stay LAST so it does not shadow the API routes) -> http://localhost:8000/
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles

_UI = _Path(__file__).resolve().parent.parent / "frontend"
if _UI.exists():
    app.mount("/", StaticFiles(directory=str(_UI), html=True), name="ui")
