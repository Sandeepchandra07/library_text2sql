from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from openai import OpenAI

from .config import settings

log = logging.getLogger(__name__)

_client = OpenAI(base_url=settings.ollama_base_url, api_key="ollama", timeout=settings.llm_timeout)


def chat(system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 1200) -> str:
    resp = _client.chat.completions.create(
        model=settings.ollama_model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def chat_stream(system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 800) -> Iterator[str]:
    stream = _client.chat.completions.create(
        model=settings.ollama_model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _extract_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def chat_json(system: str, user: str, *, max_tokens: int = 1200, retries: int = 1) -> dict[str, Any]:
    """Ask for a JSON object; retry once with a repair hint. Returns {} on failure."""
    prompt = user
    for attempt in range(retries + 1):
        try:
            resp = _client.chat.completions.create(
                model=settings.ollama_model,
                messages=[
                    {"role": "system", "content": system + "\nReturn ONLY one valid JSON object."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            data = _extract_json(resp.choices[0].message.content or "")
            if data is not None:
                return data
            prompt = user + "\n\nYour previous reply was not valid JSON. Reply with valid JSON only."
        except Exception as e:  # noqa: BLE001
            log.warning("LLM JSON call failed (attempt %s): %s", attempt + 1, e)
    return {}


def extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.S | re.I)
    sql = (m.group(1) if m else text).strip()
    return sql.rstrip(";").strip()
