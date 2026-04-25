"""Push-to-talk microphone recorder."""
from __future__ import annotations

import queue
import threading
import time

import numpy as np
import sounddevice as sd

from config import CONFIG


def resolve_input_device(spec: str) -> int | None:
    """Map a user-supplied device spec to a sounddevice index.

    Accepts an int (index), a substring of a device name, or "" for default.
    Raises ValueError with a helpful list if the spec doesn't match anything.
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.lstrip("-").isdigit():
        idx = int(spec)
        info = sd.query_devices(idx)
        if info["max_input_channels"] <= 0:
            raise ValueError(f"Device {idx} ({info['name']!r}) has no input channels.")
        return idx

    needle = spec.lower()
    matches = [
        i for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0 and needle in d["name"].lower()
    ]
    if not matches:
        available = "\n".join(
            f"  {i}: {d['name']}"
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0
        )
        raise ValueError(
            f"No input device matches {spec!r}. Available inputs:\n{available}"
        )
    return matches[0]


def describe_device(idx: int | None) -> str:
    info = sd.query_devices(idx if idx is not None else sd.default.device[0])
    src = "default" if idx is None else f"index {idx}"
    return f"{info['name']} ({src}, {int(info['max_input_channels'])}ch)"


def pick_samplerate(device: int | None, target: int) -> int:
    """Return a samplerate the device will actually accept.

    Some drivers (notably WASAPI) reject anything other than the device's
    native rate. Try `target` first, fall back to `default_samplerate`.
    """
    try:
        sd.check_input_settings(device=device, samplerate=target, channels=1, dtype="float32")
        return target
    except sd.PortAudioError:
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        return int(info["default_samplerate"])


def resample_to(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample. Fine for speech feeding Whisper."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    duration = audio.size / src_rate
    n_out = int(round(duration * dst_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    src_t = np.arange(audio.size, dtype=np.float64) / src_rate
    dst_t = np.arange(n_out, dtype=np.float64) / dst_rate
    return np.interp(dst_t, src_t, audio).astype(np.float32, copy=False)


class Recorder:
    """Streams mic audio into a buffer while `recording` is True."""

    def __init__(self) -> None:
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self._recording = False
        self._start_ts = 0.0
        self._lock = threading.Lock()
        self._device = resolve_input_device(CONFIG.input_device)
        self._capture_rate = pick_samplerate(self._device, CONFIG.sample_rate)

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            # Overflow/underflow — non-fatal, just note it.
            print(f"[audio] {status}", flush=True)
        # Copy — sounddevice reuses the buffer.
        self._q.put(indata.copy())

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._q = queue.Queue()
            self._stream = sd.InputStream(
                samplerate=self._capture_rate,
                channels=CONFIG.channels,
                dtype="float32",
                callback=self._callback,
                device=self._device,
            )
            self._stream.start()
            self._recording = True
            self._start_ts = time.monotonic()

    def stop(self) -> np.ndarray:
        """Stop and return mono float32 audio in [-1, 1]."""
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            assert self._stream is not None
            self._stream.stop()
            self._stream.close()
            self._stream = None

        chunks: list[np.ndarray] = []
        while True:
            try:
                chunks.append(self._q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if CONFIG.input_gain != 1.0:
            audio = np.clip(audio * CONFIG.input_gain, -1.0, 1.0)
        audio = resample_to(audio, self._capture_rate, CONFIG.sample_rate)
        return audio.astype(np.float32, copy=False)

    @property
    def device_description(self) -> str:
        extra = ""
        if self._capture_rate != CONFIG.sample_rate:
            extra = f", capturing @ {self._capture_rate} Hz → {CONFIG.sample_rate} Hz"
        return describe_device(self._device) + extra

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_ts if self._recording else 0.0
