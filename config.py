"""Configuration for wisperflow."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


@dataclass
class Config:
    # --- Audio ---
    sample_rate: int = 16_000
    channels: int = 1
    # Hard cap so a stuck hotkey can't fill memory.
    max_record_seconds: float = 120.0
    # Input device. Accepts an integer index ("1") or a case-insensitive
    # substring match against the device name ("realtek", "oculus"). Empty =
    # system default. Run `python main.py --list-devices` to see options.
    input_device: str = os.getenv("WISPERFLOW_INPUT_DEVICE", "")
    # Mic gain applied before transcription. 1.0 = no change. Bump if your
    # mic is quiet and Whisper is missing words.
    input_gain: float = float(os.getenv("WISPERFLOW_INPUT_GAIN", "1.0"))

    # --- Whisper ---
    # tiny/base/small/medium/large-v3 (+ .en variants). base.en is a good CPU default.
    whisper_model: str = os.getenv("WISPERFLOW_MODEL", "base.en")
    # "cpu" | "cuda" | "auto"
    whisper_device: str = os.getenv("WISPERFLOW_DEVICE", "auto")
    # int8 is fast on CPU; float16 for GPU.
    whisper_compute_type: str = os.getenv("WISPERFLOW_COMPUTE", "auto")
    whisper_language: str | None = os.getenv("WISPERFLOW_LANG", "en") or None
    # VAD filter skips silence — big speedup on pauses.
    whisper_vad: bool = True

    # --- Rewriter ---
    # "claude" | "ollama" | "none"  (none = paste raw transcription)
    rewriter: str = os.getenv("WISPERFLOW_REWRITER", "claude")
    anthropic_model: str = os.getenv("WISPERFLOW_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    rewriter_max_tokens: int = 2048
    # Ollama (local LLM). Any chat-capable model works; llama3.2 is a good default.
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("WISPERFLOW_OLLAMA_MODEL", "llama3.2:latest")
    # Low temperature = the model sticks closer to the input (what we want for polish).
    rewriter_temperature: float = float(os.getenv("WISPERFLOW_TEMPERATURE", "0.1"))

    # --- Hotkey ---
    # pynput hotkey string. Hold to record, release to transcribe.
    # Examples: "<ctrl>+<alt>+<space>", "<ctrl>+<shift>+v"
    hotkey: str = os.getenv("WISPERFLOW_HOTKEY", "<ctrl>+<alt>+<space>")

    # --- Injection ---
    # "auto" (paste in GUI apps, type in terminals), "paste" (always Ctrl+V),
    # or "type" (always simulate keystrokes). Terminals don't all accept
    # Ctrl+V as paste (mintty binds it to quoted-insert), so "auto" is safest.
    inject_mode: str = os.getenv("WISPERFLOW_INJECT", "auto")
    # On Linux, xdotool is more robust for Ctrl+V than pynput for some apps.
    linux_use_xdotool: bool = True

    # --- UX ---
    # Optional path to a short WAV to play on start/stop. Empty disables.
    start_sound: str = os.getenv("WISPERFLOW_START_SOUND", "")
    stop_sound: str = os.getenv("WISPERFLOW_STOP_SOUND", "")
    # Floating overlay that shows current state (recording/transcribing/polishing).
    show_indicator: bool = os.getenv("WISPERFLOW_INDICATOR", "1").strip().lower() in ("1", "true", "yes", "on")
    # Position: top-left | top-center | top-right | bottom-left | bottom-center | bottom-right.
    indicator_position: str = os.getenv("WISPERFLOW_INDICATOR_POS", "bottom-right")
    indicator_width: int = int(os.getenv("WISPERFLOW_INDICATOR_W", "140"))
    indicator_height: int = int(os.getenv("WISPERFLOW_INDICATOR_H", "32"))
    # Short beeps on recording start, transitions, and pipeline completion (Windows only).
    # Default ON — the visual overlay can be hidden behind fullscreen apps; the beep can't.
    beep: bool = os.getenv("WISPERFLOW_BEEP", "1").strip().lower() in ("1", "true", "yes", "on")
    # Where to write a rolling log of transcriptions (useful for tuning).
    history_path: Path = Path.home() / ".wisperflow" / "history.jsonl"

    # --- Rewriter prompt tuning ---
    # Extra user-supplied guidance appended to the system prompt.
    style_hints: str = os.getenv("WISPERFLOW_STYLE", "")


CONFIG = Config()
CONFIG.history_path.parent.mkdir(parents=True, exist_ok=True)
