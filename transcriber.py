"""Local Whisper transcription via faster-whisper."""
from __future__ import annotations

import threading
import time

import numpy as np
from faster_whisper import WhisperModel

from config import CONFIG


def _resolve_device_compute() -> tuple[str, str]:
    device = CONFIG.whisper_device
    compute = CONFIG.whisper_compute_type
    if device == "auto":
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


class Transcriber:
    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> WhisperModel:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            device, compute = _resolve_device_compute()
            print(
                f"[whisper] loading {CONFIG.whisper_model} on {device} ({compute})...",
                flush=True,
            )
            t0 = time.monotonic()
            self._model = WhisperModel(
                CONFIG.whisper_model,
                device=device,
                compute_type=compute,
            )
            print(f"[whisper] ready in {time.monotonic() - t0:.1f}s", flush=True)
            return self._model

    def warmup(self) -> None:
        """Load the model and run a tiny inference so the first real call is fast."""
        model = self._ensure_loaded()
        silence = np.zeros(int(CONFIG.sample_rate * 0.2), dtype=np.float32)
        list(model.transcribe(silence, language=CONFIG.whisper_language, beam_size=1)[0])

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size < CONFIG.sample_rate * 0.25:  # <250ms — likely a misfire
            return ""
        model = self._ensure_loaded()
        segments, _info = model.transcribe(
            audio,
            language=CONFIG.whisper_language,
            beam_size=5,
            vad_filter=CONFIG.whisper_vad,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
