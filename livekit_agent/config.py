"""Production configuration for the LiveKit voice agent worker."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name) or default)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name) or default)
    except ValueError:
        return default


def _get_bool(name: str, default: bool = True) -> bool:
    val = _get(name, "1" if default else "0").lower()
    return val in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_api_url: str
    agent_name: str
    agent_deployment: str
    agent_display_name: str
    agent_logs: bool

    # LLM (Agent reasoning — Groq for low latency, or Ollama for local)
    llm_provider: str              # "groq" | "ollama"
    groq_api_key: str
    groq_model: str
    ollama_base_url: str
    ollama_model: str

    # STT (Deepgram)
    stt_url: str                   # legacy, kept for backward compatibility
    stt_language: str              # "auto" | "hi" | "en" | "multi"
    stt_max_retries: int
    stt_retry_delay: float
    deepgram_api_key: str
    stt_model: str

    # TTS (Self-hosted Kokoro / EdgeTTS)
    tts_url: str
    tts_language: str
    tts_voice: str
    tts_speed: float
    tts_sample_rate: int
    tts_max_text_length: int       # TTS ko hang hone se bachane ke liye
    tts_max_retries: int

    # Library Backend (/ask API)
    library_agent_url: str
    library_agent_timeout: float
    library_agent_max_answer_chars: int  # Voice ke liye answer truncate karo
    library_agent_api_key: str           # X-API-Key of the text-to-SQL backend (if API_KEY is set there)
    voice_allow_anonymous: bool          # False = agent refuses data questions when no member id was passed

    # Audio Pipeline
    debug_dump_audio: bool
    token_ttl_seconds: int

    # Voice Preprocessing
    enable_markdown_cleanup: bool
    enable_number_to_words: bool


settings = Settings(
    livekit_url=_get("LIVEKIT_URL", "wss://localhost:7880"),
    livekit_api_key=_get("LIVEKIT_API_KEY"),
    livekit_api_secret=_get("LIVEKIT_API_SECRET"),
    livekit_api_url=_get("LIVEKIT_API_URL", ""),
    agent_name=_get("LIVEKIT_AGENT_NAME", "library-voice-assistant"),
    agent_deployment=_get("LIVEKIT_AGENT_DEPLOYMENT", ""),
    agent_display_name=_get("LIVEKIT_AGENT_DISPLAY_NAME", "SOUL Library Assistant"),
    agent_logs=_get_bool("LIVEKIT_AGENT_LOGS", True),

    # LLM
    llm_provider=_get("LLM_PROVIDER", "groq").lower(),
    groq_api_key=_get("GROQ_API_KEY"),
    groq_model=_get("GROQ_MODEL", "openai/gpt-oss-20b"),
    ollama_base_url=_get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
    ollama_model=_get("OLLAMA_MODEL", "qwen2.5:7b"),

    # STT (Deepgram)
    stt_url=_get("STT_SERVER_URL", "http://localhost:8229").rstrip("/"),
    stt_language=_get("STT_LANGUAGE", "multi"),
    stt_max_retries=_get_int("STT_MAX_RETRIES", 3),
    stt_retry_delay=_get_float("STT_RETRY_DELAY", 1.0),
    deepgram_api_key=_get("DEEPGRAM_API_KEY"),
    stt_model=_get("STT_MODEL", "nova-2-general"),

    # TTS
    tts_url=_get("TTS_SERVER_URL", "http://localhost:8001").rstrip("/"),
    tts_language=_get("TTS_LANGUAGE", "hi"),
    tts_voice=_get("TTS_VOICE", "hi-IN-SwaraNeural"),
    tts_speed=_get_float("TTS_SPEED", 1.0),
    tts_sample_rate=_get_int("TTS_SAMPLE_RATE", 44100),
    tts_max_text_length=_get_int("TTS_MAX_TEXT_LENGTH", 800),
    tts_max_retries=_get_int("TTS_MAX_RETRIES", 2),

    # Library
    library_agent_url=_get("LIBRARY_AGENT_URL", "http://library_text2sql.mssplonline.in").rstrip("/"),
    library_agent_timeout=_get_float("LIBRARY_AGENT_TIMEOUT", 180.0),
    library_agent_api_key=_get("LIBRARY_AGENT_API_KEY"),
    voice_allow_anonymous=_get_bool("VOICE_ALLOW_ANONYMOUS", False),
    library_agent_max_answer_chars=_get_int("LIBRARY_AGENT_MAX_ANSWER_CHARS", 3000),

    # Misc
    debug_dump_audio=_get_bool("DEBUG_DUMP_AUDIO", False),
    token_ttl_seconds=_get_int("LIVEKIT_TOKEN_TTL", 3600),
    enable_markdown_cleanup=_get_bool("ENABLE_MARKDOWN_CLEANUP", True),
    enable_number_to_words=_get_bool("ENABLE_NUMBER_TO_WORDS", False),
)
