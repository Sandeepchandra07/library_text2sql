from __future__ import annotations

import asyncio
import io
import re
import uuid
import logging
import edge_tts
import av

from livekit.agents import (
    APIConnectOptions,
    tts,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from .config import settings

DEFAULT_SAMPLE_RATE = 24000  # Edge TTS default is 24kHz
NUM_CHANNELS = 1


# ═══════════════════════════════════════════════════════════════
# TEXT PREPROCESSING: Markdown → Natural Speech
# ═══════════════════════════════════════════════════════════════

def _convert_table_to_speech(text: str) -> str:
    """Convert markdown tables to spoken sentences."""
    lines = text.split('\n')
    result_lines = []
    in_table = False
    table_rows = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|'):
            in_table = True
            if re.match(r'^\s*\|[\s\-:|]+\|\s*$', stripped):
                continue
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if cells and any(c for c in cells):
                table_rows.append(cells)
        else:
            if in_table and table_rows:
                header = table_rows[0]
                data_rows = table_rows[1:]
                if data_rows:
                    result_lines.append(
                        f"Total {len(data_rows)} records found. "
                        f"First one: {', '.join(f'{h} is {v}' for h, v in zip(header, data_rows[0]) if h and v)}."
                    )
                else:
                    result_lines.append(f"Table data: {', '.join(header)}.")
                table_rows = []
                in_table = False
            result_lines.append(line)
    
    if table_rows:
        header = table_rows[0]
        data_rows = table_rows[1:]
        if data_rows:
            result_lines.append(
                f"Total {len(data_rows)} records. "
                f"First: {', '.join(f'{h} is {v}' for h, v in zip(header, data_rows[0]) if h and v)}."
            )
    
    return '\n'.join(result_lines)


def preprocess_for_tts(text: str) -> str:
    """Convert any markdown/formatting to plain speakable text."""
    if not text or not text.strip():
        return "Koi jawab nahi mila."
    
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = _convert_table_to_speech(text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
    
    text = text.replace('₹', 'Rupees ')
    text = text.replace('Rs.', 'Rupees ')
    text = text.replace('%', ' percent')
    
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    max_len = getattr(settings, "tts_max_text_length", 500)
    if len(text) > max_len:
        truncated = text[:max_len].rsplit(' ', 1)[0]
        if not truncated:
            truncated = text[:max_len]
        text = truncated + ". Baaki details text chat mein dekh sakte hain."
    
    return text


# ═══════════════════════════════════════════════════════════════
# MP3 DECODING WORKER
# ═══════════════════════════════════════════════════════════════

def _decode_mp3_to_pcm(mp3_buffer: io.BytesIO, target_sample_rate: int) -> bytes:
    """Synchronous function to decode MP3 bytes to raw PCM s16le using PyAV."""
    container = av.open(mp3_buffer)
    audio_stream = container.streams.audio[0]
    resampler = av.AudioResampler(format='s16', layout='mono', rate=target_sample_rate)
    
    pcm_data = b""
    for frame in container.decode(audio_stream):
        resampled_frames = resampler.resample(frame)
        for r_frame in resampled_frames:
            pcm_data += r_frame.to_ndarray().tobytes()
    
    container.close()
    return pcm_data


# ═══════════════════════════════════════════════════════════════
# EDGE TTS IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════

class EdgeTTS(tts.TTS):
    """Production TTS using Microsoft Edge TTS."""

    def __init__(
        self,
        *,
        voice: str | None = None,
        sample_rate: int | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate or DEFAULT_SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        # Default to hi-IN-SwaraNeural if not provided in settings
        self._voice = voice or getattr(settings, "tts_voice", "hi-IN-SwaraNeural")

    @property
    def model(self) -> str:
        return "Edge-TTS"

    @property
    def provider(self) -> str:
        return "Microsoft"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        # 🧹 Preprocess text before TTS
        clean_text = preprocess_for_tts(text) if getattr(settings, "enable_markdown_cleanup", True) else text
        return EdgeTTSChunkedStream(tts=self, input_text=clean_text, conn_options=conn_options)


class EdgeTTSChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: EdgeTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: EdgeTTS = tts
        self._logger = logging.getLogger("soul.tts")

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = uuid.uuid4().hex[:16]
        sample_rate = self._tts._sample_rate

        self._logger.info("TTS synthesize | chars=%d text=%.50s...", len(self._input_text), self._input_text)

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )

        try:
            # Initialize Edge TTS communicate object
            communicate = edge_tts.Communicate(self._input_text, self._tts._voice)
            
            # 1. Fetch MP3 chunks from Edge TTS and write to in-memory buffer
            buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.write(chunk["data"])
            
            # 2. Decode MP3 to PCM in a separate thread to avoid blocking the async event loop
            buffer.seek(0)
            pcm_bytes = await asyncio.to_thread(_decode_mp3_to_pcm, buffer, sample_rate)
            
            # 3. Push raw PCM to LiveKit audio emitter
            if pcm_bytes:
                output_emitter.push(pcm_bytes)
            
            output_emitter.flush()
            self._logger.info("TTS completed | request_id=%s bytes=%d", request_id, len(pcm_bytes))
            
        except Exception as e:
            self._logger.error("Edge TTS synthesis failed: %s", e)
            # Fail gracefully, LiveKit will just play silence for this chunk
            output_emitter.flush()