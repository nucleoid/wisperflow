"""wisperflow — push-to-talk dictation with local Whisper + LLM polish.

Hold the hotkey, speak, release. The transcription is rewritten into
polished prose and pasted into whatever app has focus.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from config import CONFIG
from rewriter import Rewriter

if TYPE_CHECKING:
    import numpy as np


class App:
    def __init__(self) -> None:
        # Import lazily so --text mode works without audio/X11 libs.
        from injector import inject
        from recorder import Recorder
        from transcriber import Transcriber

        from indicator import Indicator

        self.recorder = Recorder()
        self.transcriber = Transcriber()
        self.rewriter = Rewriter()
        self._inject = inject
        self.indicator = Indicator(
            show=CONFIG.show_indicator,
            beep=CONFIG.beep,
            position=CONFIG.indicator_position,
            width=CONFIG.indicator_width,
            height=CONFIG.indicator_height,
        )
        # Serialize the heavy work so a second tap can't race an in-flight job.
        self._work_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._keyboard = None  # pynput.keyboard module, loaded in start()
        self._release_keys: frozenset = frozenset()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        from pynput import keyboard  # noqa: PLC0415 — lazy so --text works headless

        self._keyboard = keyboard
        self._release_keys = frozenset(
            {
                keyboard.Key.space,
                keyboard.Key.ctrl,
                keyboard.Key.ctrl_l,
                keyboard.Key.ctrl_r,
                keyboard.Key.alt,
                keyboard.Key.alt_l,
                keyboard.Key.alt_r,
                keyboard.Key.shift,
                keyboard.Key.shift_l,
                keyboard.Key.shift_r,
                keyboard.Key.cmd,
                keyboard.Key.cmd_l,
                keyboard.Key.cmd_r,
            }
        )

        print(f"[wisperflow] mic: {self.recorder.device_description}", flush=True)
        print("[wisperflow] warming up Whisper...", flush=True)
        self.transcriber.warmup()
        print(
            f"[wisperflow] ready. Hold {CONFIG.hotkey} to dictate. Ctrl+C to quit.",
            flush=True,
        )

        self._listener = None
        self._raw = None
        self._win_hotkey = None
        # Windows: use a low-level hook that suppresses the chord so the
        # action key doesn't leak into the focused app (PowerShell would
        # see extra spaces; mintty would flash white from the readline bell).
        if sys.platform == "win32":
            try:
                from hotkey import WindowsPushToTalk, parse_hotkey  # noqa: PLC0415
                if parse_hotkey(CONFIG.hotkey) is not None:
                    self._win_hotkey = WindowsPushToTalk(
                        CONFIG.hotkey,
                        on_press=self._on_hotkey_press,
                        on_release=self._on_chord_release,
                    )
                    self._win_hotkey.start()
                    print("[wisperflow] hotkey: low-level hook (suppressing action key)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[wisperflow] hotkey suppression unavailable: {e!r}", flush=True)
                self._win_hotkey = None

        if self._win_hotkey is None:
            # Fallback: pynput's GlobalHotKeys. Does not suppress the chord
            # from the focused window — non-Windows, or unparseable hotkey.
            self._listener = keyboard.GlobalHotKeys(
                {CONFIG.hotkey: self._on_hotkey_press},
            )
            self._raw = keyboard.Listener(on_release=self._on_any_release)
            self._listener.start()
            self._raw.start()

        signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
        # Poll instead of blocking forever — on Windows, an untimed wait
        # prevents Python from delivering SIGINT to the main thread.
        while not self._stop_event.wait(0.5):
            pass
        self._shutdown()

    def _shutdown(self) -> None:
        print("\n[wisperflow] shutting down...", flush=True)
        try:
            if self._win_hotkey is not None:
                self._win_hotkey.stop()
            if self._listener is not None:
                self._listener.stop()
            if self._raw is not None:
                self._raw.stop()
        except Exception:
            pass
        if self.recorder.is_recording:
            self.recorder.stop()
        self.indicator.stop()

    # --- hotkey handling --------------------------------------------------

    def _on_hotkey_press(self) -> None:
        if self.recorder.is_recording:
            return
        self.indicator.set_state("recording")
        print("[wisperflow] ● recording...", flush=True)
        self.recorder.start()
        # Safety net: auto-stop if somehow we miss the release event.
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog.start()

    def _on_any_release(self, key) -> None:
        # Any modifier release ends the push-to-talk window. This is simple
        # and works well: you lift any finger off the chord and dictation stops.
        if not self.recorder.is_recording:
            return
        assert self._keyboard is not None
        if not (isinstance(key, self._keyboard.Key) and key in self._release_keys):
            return
        # Stop the recorder synchronously so the watchdog exits cleanly and a
        # second release event doesn't spawn a redundant worker.
        audio = self.recorder.stop()
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _on_chord_release(self) -> None:
        # Windows hook path: fires when the action key goes up or any chord
        # modifier is released while the chord is active.
        if not self.recorder.is_recording:
            return
        audio = self.recorder.stop()
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _watchdog_loop(self) -> None:
        while self.recorder.is_recording:
            if self.recorder.elapsed > CONFIG.max_record_seconds:
                print("[wisperflow] max record time hit — stopping.", flush=True)
                audio = self.recorder.stop()
                threading.Thread(target=self._process, args=(audio,), daemon=True).start()
                return
            time.sleep(0.25)

    # --- pipeline ---------------------------------------------------------

    def _process(self, audio: "np.ndarray") -> None:
        if not self._work_lock.acquire(blocking=False):
            print("[wisperflow] busy — dropped overlapping dictation.", flush=True)
            return
        try:
            if audio.size == 0:
                print("[wisperflow] no audio captured.", flush=True)
                return
            duration = audio.size / CONFIG.sample_rate
            import numpy as np  # noqa: PLC0415
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
            print(
                f"[wisperflow] ⏹ captured {duration:.1f}s  peak={peak:.3f} rms={rms:.3f}",
                flush=True,
            )
            if peak < 0.005:
                print(
                    "[wisperflow] audio is silent — wrong mic, muted, or no Windows "
                    "permission. Try `python main.py --list-devices` and set "
                    "WISPERFLOW_INPUT_DEVICE in .env.",
                    flush=True,
                )

            self.indicator.set_state("transcribing")
            t0 = time.monotonic()
            raw = self.transcriber.transcribe(audio)
            t_trans = (time.monotonic() - t0) * 1000
            if not raw:
                print("[wisperflow] (nothing transcribed)", flush=True)
                return
            print(f"[wisperflow] raw ({t_trans:.0f}ms): {raw!r}", flush=True)

            self.indicator.set_state("polishing")
            result = self.rewriter.rewrite(raw)
            if result.used_llm:
                print(
                    f"[wisperflow] polished ({result.latency_ms:.0f}ms): {result.text!r}",
                    flush=True,
                )
            else:
                print("[wisperflow] rewriter disabled — using raw text.", flush=True)

            self._inject(result.text)
            self._log(duration, raw, result.text, t_trans, result.latency_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[wisperflow] ERROR: {e!r}", flush=True)
        finally:
            self.indicator.set_state("idle")
            self._work_lock.release()

    def _log(
        self,
        duration_s: float,
        raw: str,
        polished: str,
        transcribe_ms: float,
        rewrite_ms: float,
    ) -> None:
        try:
            with CONFIG.history_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "duration_s": round(duration_s, 2),
                            "transcribe_ms": round(transcribe_ms),
                            "rewrite_ms": round(rewrite_ms),
                            "raw": raw,
                            "polished": polished,
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass


def _list_devices() -> None:
    import sounddevice as sd  # noqa: PLC0415
    from recorder import resolve_input_device  # noqa: PLC0415

    devices = sd.query_devices()
    default_in = sd.default.device[0]
    print("Input devices (set WISPERFLOW_INPUT_DEVICE to an index or name substring):\n")
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0:
            continue
        marker = "*" if i == default_in else " "
        api = sd.query_hostapis(d["hostapi"])["name"]
        print(f"  {marker} {i:>2}: {d['name']}  [{api}, {d['max_input_channels']}ch]")
    print("\n* = system default")
    if CONFIG.input_device:
        try:
            idx = resolve_input_device(CONFIG.input_device)
            print(f"\nCurrent WISPERFLOW_INPUT_DEVICE={CONFIG.input_device!r} resolves to index {idx}.")
        except ValueError as e:
            print(f"\nWARNING: {e}")


def _mic_test(seconds: float) -> None:
    import sounddevice as sd  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from recorder import describe_device, pick_samplerate, resolve_input_device  # noqa: PLC0415

    try:
        device = resolve_input_device(CONFIG.input_device)
    except ValueError as e:
        print(f"[wisperflow] {e}")
        return

    capture_rate = pick_samplerate(device, CONFIG.sample_rate)
    print(f"[wisperflow] mic: {describe_device(device)}")
    print(f"[wisperflow] capture rate: {capture_rate} Hz (target {CONFIG.sample_rate} Hz)")
    print(f"[wisperflow] gain: {CONFIG.input_gain}x")
    print(f"[wisperflow] speak now — recording {seconds:.1f}s. Watch the level bar.\n")

    peak = 0.0
    rms_sum = 0.0
    rms_n = 0

    def cb(indata, frames, time_info, status):  # noqa: ARG001
        nonlocal peak, rms_sum, rms_n
        if status:
            print(f"[audio] {status}", flush=True)
        block = indata
        if block.ndim > 1:
            block = block.mean(axis=1)
        block = block * CONFIG.input_gain
        block_peak = float(np.max(np.abs(block))) if block.size else 0.0
        block_rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
        peak = max(peak, block_peak)
        rms_sum += block_rms
        rms_n += 1
        bars = int(min(block_rms * 50, 40))
        meter = "#" * bars + "-" * (40 - bars)
        print(f"\r  [{meter}] rms={block_rms:.3f} peak={block_peak:.3f}", end="", flush=True)

    with sd.InputStream(
        samplerate=capture_rate,
        channels=CONFIG.channels,
        dtype="float32",
        callback=cb,
        device=device,
    ):
        time.sleep(seconds)
    print()
    avg_rms = rms_sum / rms_n if rms_n else 0.0
    print(f"\n[wisperflow] done. peak={peak:.3f}, avg_rms={avg_rms:.3f}")
    if peak < 0.01:
        print(
            "[wisperflow] signal looks silent. Wrong device, mic muted, or Windows mic "
            "permission off (Settings -> Privacy -> Microphone)."
        )
    elif peak < 0.05:
        print("[wisperflow] very quiet — try raising WISPERFLOW_INPUT_GAIN (e.g. 3.0).")
    else:
        print("[wisperflow] mic looks good.")


def main() -> None:
    parser = argparse.ArgumentParser(description="wisperflow — local dictation with LLM polish")
    parser.add_argument(
        "--once",
        metavar="SECONDS",
        type=float,
        help="Record SECONDS from the mic, print the result, and exit (no hotkey).",
    )
    parser.add_argument(
        "--text",
        metavar="STRING",
        help="Skip audio: just run the rewriter on STRING and print the result.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices and exit.",
    )
    parser.add_argument(
        "--mic-test",
        nargs="?",
        const=10.0,
        type=float,
        metavar="SECONDS",
        help="Open the configured mic and show a live level meter (default 10s). "
             "Use this to confirm the right device is selected and picking up audio.",
    )
    args = parser.parse_args()

    if args.list_devices:
        _list_devices()
        return

    if args.mic_test is not None:
        _mic_test(args.mic_test)
        return

    if args.text is not None:
        rw = Rewriter()
        result = rw.rewrite(args.text)
        print(result.text)
        return

    app = App()

    if args.once is not None:
        app.transcriber.warmup()
        print(f"[wisperflow] recording {args.once}s...", flush=True)
        app.recorder.start()
        time.sleep(args.once)
        audio = app.recorder.stop()
        raw = app.transcriber.transcribe(audio)
        print(f"raw: {raw}")
        result = app.rewriter.rewrite(raw)
        print(f"polished: {result.text}")
        return

    app.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
