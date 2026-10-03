from __future__ import annotations

import asyncio
import difflib
import logging
import math
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
from livekit.agents import (
    APIConnectOptions,
    APITimeoutError,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given

from .config import settings

_logger = logging.getLogger("soul.stt")

_DUMP_DIR = Path(__file__).resolve().parent.parent / "voice-tmp"
if settings.debug_dump_audio:
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_NUM_CHANNELS = 1

# --------------------------------------------------------------------------
# Tunables (override from settings/.env if you want; defaults are demo-safe)
# --------------------------------------------------------------------------
DEFAULT_LANGUAGE = "hi"
TARGET_SAMPLE_RATE = 16000        # Whisper's native rate; we resample here, once, properly
MIN_CONFIDENCE_THRESHOLD = float(getattr(settings, "stt_min_confidence", 0.35))
MIN_AUDIO_SECONDS = 0.25          # ignore clicks / very short blips
PAD_SECONDS = 0.25                # silence added before and after audio
NOISE_FLOOR_RMS = 60.0            # int16 RMS below this = near silence, don't boost
MAX_GAIN_DB = 12.0                # never boost more than this

# SOUL library words the agent/SQL layer depends on. Close mis-hearings of these
# get snapped to the correct spelling. Only distinctive (4+ letter) words here:
# short common Hindi words must never be in this list.
GLOSSARY = [
    "एपिसोड", "वीडियो", "वैल्यू", "टोटल", "मेंबर", "आईडी",
    "इश्यू", "रिटर्न", "रिन्यू", "फाइन", "ओवरड्यू", "एक्सेशन",
    "कैटलॉग", "ओपैक", "रिमाइंडर", "बारकोड", "रिज़र्व",
    "अवेलेबल", "लाइब्रेरी", "पाइथन", "इश्यूड",
    # more SOUL library words (no token limit here, unlike Whisper's prompt)
    "लाइब्रेरियन", "रिन्यूअल", "रिज़र्वेशन", "रिटर्न्ड", "डिफॉल्टर", "मेंबरशिप",
    "डिपार्टमेंट", "सेमेस्टर", "एनरोलमेंट", "आईएसबीएन", "एडिशन", "पब्लिशर",
    "ऑथर", "टाइटल", "कीवर्ड", "अनअवेलेबल", "कॉपीज़", "बैलेंस", "जर्नल्स",
    "मैगज़ीन", "थीसिस", "डिजिटल", "रिसोर्स", "पीडीएफ", "ऑडियो", "डेटाबेस",
    "एल्गोरिदम", "इंजीनियरिंग", "मैनेजमेंट", "फैकल्टी", "स्टूडेंट", "नेटवर्किंग",
]

# 4-letter words (फाइन, टोटल, आईडी...) are too close to real words (फाइनल) for fuzzy
# matching, so only 5+ letter glossary words are fuzzy-snapped. Short ones are
# fixed through PHRASE_FIXES instead.
_FUZZY_POOL = [g for g in GLOSSARY if len(g) >= 5]

_MEMBER_CONTEXT = r"\s+(टोटल|नाम|नंबर|मेंबर|आईडी|कितन|फाइन|इश्यू|किताब)"

# (wrong, right, optional "must be followed by" regex). Add more from your logs
# (look for STT_RESULT / STT_CORRECTED lines where the text is still wrong).
PHRASE_FIXES: list[tuple[str, str, str | None]] = [
    # seen in real demo screenshots
    ("गुफिस्सोड", "एपिसोड", None),
    ("वीडिया", "वीडियो", None),
    ("इस रूप", "वैल्यू", r"\s+कितन"),        # "इस रूप कितनी है" -> "वैल्यू कितनी है"
    ("वारा", "मेरा", _MEMBER_CONTEXT),         # "वारा टोटल" -> "मेरा टोटल"
    # common Whisper spellings normalised to what the DB/agent expects
    ("मेम्बर", "मेंबर", None),
    ("एपीसोड", "एपिसोड", None),
    ("एपिसोट", "एपिसोड", None),
    ("इशू", "इश्यू", None),
    ("रिनू", "रिन्यू", None),
    # seen in the whisper log
    ("इस सुड", "इश्यूड", None),
    ("इसूड", "इश्यूड", None),
    ("लाइबर रून", "लाइब्रेरी में", None),
    ("लाइबरेरी", "लाइब्रेरी", None),
    ("लायब्रेरी", "लाइब्रेरी", None),
    ("पाइटन", "पाइथन", None),
    ("मेरी बारे में", "मेरे बारे में", None),
]

# Anchored fixes: the first word is often clipped by VAD, so restore it.
REGEX_FIXES: list[tuple[str, str]] = [
    (r"^नाम और (मेरा )?(मेंबर|नंबर|आईडी)", r"मेरा नाम और मेरा \2"),
    (r"^(मेंबर|नंबर) आईडी क्या", r"मेरा \1 आईडी क्या"),
    # "बुक अवेलेबल है" is heard as "बुक आउ लेबल है" / "बुक्स और लेवल है"
    (r"(?<!\S)(?:आउ|आऊ|और|अव)\s*ले[वब]ल(?=\s+(?:है|हैं|हो)(?![^\s\.,?!।])|[\s\.,?!।]*$)", "अवेलेबल"),
]
_EDGE = r"(?<!\S)"                  # \b does not work with Devanagari matras
_END = r"(?=[\s\.,?!।]|$)"


def _apply_corrections(text: str) -> str:
    """Fix known domain-word mis-hearings. Deterministic, no network."""
    out = text
    for wrong, right, follow in PHRASE_FIXES:
        tail = rf"(?={follow})" if follow else _END
        out = re.sub(rf"{_EDGE}{re.escape(wrong)}{tail}", right, out)
    for pattern, repl in REGEX_FIXES:
        out = re.sub(pattern, repl, out.strip())

    # Fuzzy snap of single tokens to the glossary (cutoff kept high on purpose).
    parts = re.findall(r"\S+|\s+", out)
    fixed: list[str] = []
    for part in parts:
        core = part.strip(".,?!।")
        if len(core) >= 5 and core not in GLOSSARY and not part.isspace():
            match = difflib.get_close_matches(core, _FUZZY_POOL, n=1, cutoff=0.8)
            # length guard: "लाइब्रेरियन" must never become "लाइब्रेरी", plurals stay as they are
            if match and abs(len(core) - len(match[0])) <= 1:
                part = part.replace(core, match[0])
        fixed.append(part)
    return "".join(fixed)


def _normalize_audio(pcm: bytes, target_db: float = -20.0) -> bytes:
    """Bring speech to a consistent level without amplifying noise or clipping."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return pcm
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms < NOISE_FLOOR_RMS:
        return pcm  # basically silence/noise: boosting only creates hallucinations
    current_db = 20 * np.log10(rms / 32768.0)
    gain_db = min(target_db - current_db, MAX_GAIN_DB)
    gain = 10 ** (gain_db / 20.0)
    peak = float(np.max(np.abs(samples)))
    if peak * gain > 32767 * 0.95:  # limiter instead of hard clipping
        gain = (32767 * 0.95) / peak
    return np.clip(samples * gain, -32768, 32767).astype(np.int16).tobytes()


def _resample_pcm(pcm: bytes, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """mono s16le -> mono s16le at dst_rate, with anti-alias filtering."""
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(np.float32)
    try:
        from scipy.signal import resample_poly

        g = math.gcd(src_rate, dst_rate)
        y = resample_poly(x, dst_rate // g, src_rate // g)
    except ImportError:  # numpy-only fallback: windowed-sinc low-pass + interpolation
        if src_rate > dst_rate:
            cutoff = 0.5 * dst_rate / src_rate
            n = 101
            t = np.arange(n) - (n - 1) / 2
            h = np.sinc(2 * cutoff * t) * np.hamming(n)
            x = np.convolve(x, h / h.sum(), mode="same")
        y = np.interp(np.arange(0, x.size, src_rate / dst_rate), np.arange(x.size), x)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _pad_silence(pcm: bytes, sample_rate: int) -> bytes:
    """Whisper often drops the first word if audio starts abruptly."""
    pad = np.zeros(int(sample_rate * PAD_SECONDS), dtype=np.int16).tobytes()
    return pad + pcm + pad


def _to_mono_pcm16(buffer: utils.audio.AudioBuffer) -> tuple[bytes, int]:
    """Flatten incoming frames into mono s16le PCM."""
    frame = utils.audio.combine_frames(buffer)
    sample_rate = frame.sample_rate
    num_channels = frame.num_channels
    raw = frame.data
    if isinstance(raw, memoryview):
        raw = raw.tobytes()

    if num_channels <= 1:
        return bytes(raw), sample_rate

    samples = np.frombuffer(raw, dtype=np.int16)
    samples = samples.reshape(-1, num_channels)
    mono = samples.mean(axis=1).astype(np.int16)
    return mono.tobytes(), sample_rate


def _dump_wav(pcm: bytes, sample_rate: int, request_id: str) -> None:
    """Write raw mono s16le PCM to a WAV file under voice-tmp/."""
    filename = _DUMP_DIR / f"{request_id}.wav"
    data_size = (len(pcm) // 2) * 2
    with open(filename, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))            # PCM
        f.write(struct.pack("<H", 1))            # mono
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", sample_rate * 2))
        f.write(struct.pack("<H", 2))            # block align
        f.write(struct.pack("<H", 16))           # bits per sample
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm)
    _logger.info("dumped STT audio -> %s (%d Hz, %d bytes)", filename.name, sample_rate, len(pcm))


def _empty_event(request_id: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        request_id=request_id,
        alternatives=[],
    )


_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class WhisperHTTPSTT(stt.STT):
    """Production non-streaming STT with retry, normalization and confidence gate."""

    def __init__(
        self,
        *,
        url: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
        sample_rate: int | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._url = (url or settings.stt_url).rstrip("/")
        self._session = http_session
        self._sample_rate = sample_rate

    @property
    def model(self) -> str:
        return "faster-whisper"

    @property
    def provider(self) -> str:
        return "whisper-http"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def _get_retry_config(self) -> tuple[int, float, float]:
        max_retries = getattr(settings, "stt_max_retries", 2)
        base_delay = getattr(settings, "stt_retry_delay", 0.3)
        timeout = getattr(settings, "stt_timeout", 15.0)
        return max_retries, base_delay, timeout

    def _build_params(self, language: NotGivenOr[str]) -> dict[str, str]:
        params: dict[str, str] = {}
        if is_given(language) and language:
            params["language"] = language
        else:
            lang = getattr(settings, "stt_language", "") or ""
            # "multi"/"auto"/"" used to mean auto-detect, which mangles Hinglish.
            params["language"] = lang if lang not in ("multi", "auto", "") else DEFAULT_LANGUAGE
        prompt = getattr(settings, "stt_initial_prompt", "") or ""
        if prompt:  # otherwise the whisper server uses its own library prompt + vocab file
            params["initial_prompt"] = prompt
        return params

    async def _recognize_impl(
        self,
        buffer: utils.audio.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        # 1. Convert, skip blips, normalize, pad
        pcm, frame_sample_rate = _to_mono_pcm16(buffer)
        request_id = uuid.uuid4().hex[:16]
        # `sample_rate` ctor arg = TARGET rate we send (default 16 kHz). Frames are
        # always resampled from their real rate, so the label can never lie.
        sample_rate = self._sample_rate or TARGET_SAMPLE_RATE
        started = time.perf_counter()

        if len(pcm) / 2 / frame_sample_rate < MIN_AUDIO_SECONDS:
            _logger.info("STT_SKIP | audio shorter than %.2fs", MIN_AUDIO_SECONDS)
            return _empty_event(request_id)

        pcm = _resample_pcm(pcm, frame_sample_rate, sample_rate)
        pcm = _pad_silence(_normalize_audio(pcm), sample_rate)

        # 2. Debug dump (this is EXACTLY what whisper receives: listen to it!)
        if settings.debug_dump_audio:
            try:
                _dump_wav(pcm, sample_rate, request_id)
            except Exception:
                _logger.debug("failed to dump audio for %s", request_id, exc_info=True)

        # 3. Build request
        params = self._build_params(language)
        params["request_id"] = request_id
        params["sample_rate"] = str(sample_rate)  # for servers that read it from the query
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Sample-Rate": str(sample_rate),
        }

        max_retries, base_delay, timeout = self._get_retry_config()
        actual_timeout = conn_options.timeout or timeout

        # 4. Retry loop (retryable errors only)
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                async with self._ensure_session().post(
                    f"{self._url}/transcribe-pcm",
                    data=pcm,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=actual_timeout),
                ) as resp:
                    if resp.status in _RETRYABLE_STATUS:
                        last_error = APIStatusError(
                            message=f"whisper STT HTTP {resp.status}",
                            status_code=resp.status,
                            retryable=True,
                        )
                        _logger.warning("STT HTTP %d (attempt %d/%d)", resp.status, attempt, max_retries)
                    else:
                        resp.raise_for_status()  # 4xx -> not retryable
                        payload = await resp.json(content_type=None)
                        break
            except asyncio.TimeoutError:
                last_error = APITimeoutError()
                _logger.warning("STT timeout (attempt %d/%d)", attempt, max_retries)
            except aiohttp.ClientResponseError as e:
                raise APIStatusError(
                    message=e.message, status_code=e.status, body=e.message, retryable=False
                ) from None
            except aiohttp.ClientError as e:
                last_error = APIStatusError(message=str(e), status_code=0, retryable=True)
                _logger.warning("STT connection error (attempt %d/%d): %s", attempt, max_retries, e)

            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

        if payload is None:
            _logger.error("STT failed after %d attempts", max_retries)
            raise last_error or APIStatusError(message="STT failed after retries", status_code=0)

        # 5. Parse
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("success") is False:
            _logger.error("STT server reported failure: %s", payload.get("message"))
            return _empty_event(request_id)
        # accept both {"data": {...}} and a flat {...} response
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_transcript = str(data.get("transcript") or "").strip()
        detected_language = str(data.get("language") or DEFAULT_LANGUAGE)
        conf_raw = data.get("confidence")
        confidence = float(conf_raw) if conf_raw is not None else None  # None = server gave none
        latency_ms = round((time.perf_counter() - started) * 1000)

        if not raw_transcript:
            return _empty_event(request_id)

        # 6. Confidence gate (low threshold; Hindi/Hinglish scores are lower than English)
        if confidence is not None and confidence < MIN_CONFIDENCE_THRESHOLD:
            _logger.warning(
                "STT_LOW_CONFIDENCE | conf=%.2f < %.2f | text=%r | dropped",
                confidence, MIN_CONFIDENCE_THRESHOLD, raw_transcript,
            )
            return _empty_event(request_id)

        # 7. Garbage / repetition filter
        words = raw_transcript.split()
        if len(words) > 3 and len(set(words)) / len(words) < 0.4:
            _logger.warning("STT_GARBAGE | text=%r | dropped", raw_transcript)
            return _empty_event(request_id)

        # 8. Domain post-correction
        transcript = _apply_corrections(raw_transcript)
        if transcript != raw_transcript:
            _logger.info("STT_CORRECTED | %r -> %r", raw_transcript, transcript)

        _logger.info(
            "STT_RESULT | lang=%s conf=%s latency=%dms text=%r",
            detected_language, "n/a" if confidence is None else f"{confidence:.2f}", latency_ms, transcript,
        )

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[
                stt.SpeechData(
                    language=detected_language,
                    text=transcript,
                    confidence=1.0 if confidence is None else confidence,
                    start_time=0.0,
                    end_time=time.perf_counter() - started,
                )
            ],
        )