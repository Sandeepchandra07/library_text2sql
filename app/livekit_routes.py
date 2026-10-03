from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

router = APIRouter(prefix="/livekit", tags=["livekit"])

TOKEN_TTL = timedelta(hours=1)


class TokenRequest(BaseModel):
    # Frontend sends all of these aliases; whichever is present is used.
    mem_cd: Optional[str] = None
    member_code: Optional[str] = None
    user_id: Optional[str] = None
    thread_id: Optional[str] = None
    name: Optional[str] = None
    display_name: Optional[str] = None


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s)[:40]


def _mint(mem_cd: str, thread_id: str, name: str) -> dict:
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    agent_name = os.getenv("AGENT_NAME") or os.getenv("LIVEKIT_AGENT_NAME") or "library-voice-assistant"

    if not (url and key and secret):
        raise HTTPException(500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET set nahi hain (backend .env).")

    allow_anon = os.getenv("VOICE_ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes")
    if not mem_cd and not allow_anon:
        raise HTTPException(400, "Member ID (mem_cd) chahiye. Frontend Settings me Member ID daalein.")

    uid = _safe(mem_cd) or "guest"
    suffix = uuid.uuid4().hex[:6]
    # Har session ke liye NAYA room: agent dispatch hamesha fresh room par hota hai, aur
    # stop -> start karne par purane room/identity se clash (DUPLICATE_IDENTITY) nahi hota.
    room = f"lib-voice-{uid}-{suffix}"
    identity = f"user-{uid}-{suffix}"
    thread = thread_id or f"voice-{uid}"

    # agent.py yahi metadata padhta hai: job.metadata -> user_id / thread_id
    metadata = json.dumps({"user_id": mem_cd, "thread_id": thread, "name": name}, ensure_ascii=False)

    token = (
        AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name or uid)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room,
                can_subscribe=True,       # agent ki awaaz sunne ke liye
                can_publish=False,        # mic publish nahi hota (STT browser me hai)
                can_publish_data=True,    # user-transcript text stream ke liye ZAROORI
            )
        )
        .with_room_config(
            RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)]
            )
        )
        .to_jwt()
    )

    return {
        "token": token,
        "url": url,
        "room": room,
        "identity": identity,
        "agent_name": agent_name,
        "expires_in": int(TOKEN_TTL.total_seconds()),
    }


@router.post("/token")
async def create_token_post(req: TokenRequest):
    mem_cd = (req.mem_cd or req.member_code or req.user_id or "").strip()
    name = (req.name or req.display_name or "").strip()
    return _mint(mem_cd, (req.thread_id or "").strip(), name)


@router.get("/token")
async def create_token_get(
    mem_cd: str = Query(""),
    thread_id: str = Query(""),
    name: str = Query(""),
):
    return _mint(mem_cd.strip(), thread_id.strip(), name.strip())