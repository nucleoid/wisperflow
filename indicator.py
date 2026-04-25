"""Visual + audio status indicator for dictation state.

A small pill-shaped overlay with an animated state icon (pulsing bars
while recording, a spinning arc while transcribing/polishing). Hides
when idle. State changes from worker threads are delivered through a
thread-safe queue; the Tk thread polls it via a self-scheduled
`after()` so no Tcl object is ever touched from a foreign thread —
otherwise Tcl panics with "Tcl_AsyncDelete: async handler deleted by
the wrong thread" at shutdown.
"""
from __future__ import annotations

import math
import platform
import queue
import threading

# Per-state (label, accent color).
_PALETTE = {
    "recording":    ("listening",    "#ff5c5c"),
    "transcribing": ("transcribing", "#f0a040"),
    "polishing":    ("polishing",    "#5aa8ff"),
}

_BG      = "#1a1a1d"   # pill body
_BG_EDGE = "#2c2c34"   # 1px highlight along the top
_FG      = "#d8d8e0"   # label text
# Color-keyed out as transparent so the corners look rounded against
# whatever's behind the window. Magenta is unlikely to clash.
_TRANSP  = "#ff00ff"

_VALID_POSITIONS = {
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
}


def _blend(c1: str, c2: str, t: float) -> str:
    """Linear RGB interpolation. t=0 -> c1, t=1 -> c2. Used to fake alpha
    by blending the accent toward the pill background — Tk canvas has no
    per-element opacity."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _resolve_position(pos: str) -> str:
    pos = (pos or "").strip().lower().replace("_", "-")
    if pos in _VALID_POSITIONS:
        return pos
    aliases = {
        "top": "top-center", "bottom": "bottom-right",
        "left": "bottom-left", "right": "bottom-right",
        "center": "bottom-center", "centre": "bottom-center",
    }
    return aliases.get(pos, "bottom-right")


def _work_area(sw: int, sh: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor's work area —
    the screen minus the taskbar. Lets us anchor right above the clock
    without hard-coding the taskbar height."""
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            SPI_GETWORKAREA = 0x0030
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            ):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return 0, 0, sw, sh


class Indicator:
    """Floating pill-shaped status overlay with optional sound cues.

    States: "idle" (hidden), "recording", "transcribing", "polishing".
    """

    def __init__(
        self,
        *,
        show: bool = True,
        beep: bool = False,
        position: str = "bottom-right",
        width: int = 140,
        height: int = 32,
    ) -> None:
        self._show = show
        self._beep = beep and platform.system() == "Windows"
        self._position = _resolve_position(position)
        self._w = max(96, int(width))
        self._h = max(22, int(height))
        self._tk = None
        self._canvas = None
        self._state = "idle"
        self._tick = 0
        self._anim_job = None
        self._poll_job = None
        self._ready = threading.Event()
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        if self._show:
            self._thread = threading.Thread(target=self._run, daemon=True, name="indicator-tk")
            self._thread.start()
            # Don't block startup if Tk is unavailable.
            self._ready.wait(timeout=2.0)

    # --- geometry ---------------------------------------------------------

    def _compute_xy(self, sw: int, sh: int) -> tuple[int, int]:
        wleft, wtop, wright, wbottom = _work_area(sw, sh)
        gap_edge, gap_top = 8, 24
        vpos, hpos = self._position.split("-")
        if hpos == "left":
            x = wleft + gap_edge
        elif hpos == "right":
            x = wright - self._w - gap_edge
        else:
            x = wleft + (wright - wleft - self._w) // 2
        y = wtop + gap_top if vpos == "top" else wbottom - self._h - gap_edge
        return x, y

    # --- Tk lifecycle -----------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as e:  # noqa: BLE001
            print(f"[wisperflow] indicator disabled (tkinter unavailable: {e})", flush=True)
            self._ready.set()
            return
        try:
            self._tk = tk.Tk()
            self._tk.title("wisperflow")
            self._tk.overrideredirect(True)
            self._tk.attributes("-topmost", True)
            try:
                self._tk.attributes("-transparentcolor", _TRANSP)
            except Exception:
                pass
            try:
                self._tk.attributes("-alpha", 0.93)
            except Exception:
                pass

            sw = self._tk.winfo_screenwidth()
            sh = self._tk.winfo_screenheight()
            x, y = self._compute_xy(sw, sh)
            self._tk.geometry(f"{self._w}x{self._h}+{x}+{y}")
            self._tk.configure(bg=_TRANSP)

            self._canvas = tk.Canvas(
                self._tk,
                width=self._w,
                height=self._h,
                bg=_TRANSP,
                highlightthickness=0,
                bd=0,
            )
            self._canvas.pack(fill="both", expand=True)
            self._tk.withdraw()
            print(
                f"[wisperflow] indicator: {self._position} pill "
                f"({self._w}x{self._h}, beep={'on' if self._beep else 'off'})",
                flush=True,
            )
            self._ready.set()
            self._poll_job = self._tk.after(40, self._poll_queue)
            self._tk.mainloop()
        except Exception as e:  # noqa: BLE001
            print(f"[wisperflow] indicator crashed: {e!r}", flush=True)
            self._ready.set()
        finally:
            # All Tcl teardown happens on this (Tk-owning) thread. If destroy
            # ran from any other thread Tcl panics with "Tcl_AsyncDelete:
            # async handler deleted by the wrong thread" at shutdown.
            for job_attr in ("_anim_job", "_poll_job"):
                job = getattr(self, job_attr, None)
                if job is not None and self._tk is not None:
                    try:
                        self._tk.after_cancel(job)
                    except Exception:
                        pass
                setattr(self, job_attr, None)
            try:
                if self._tk is not None:
                    self._tk.destroy()
            except Exception:
                pass
            self._canvas = None
            self._tk = None

    def _poll_queue(self) -> None:
        """Drain pending state changes on the Tk thread. Re-schedules itself."""
        if self._tk is None:
            return
        stopping = False
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg == "__STOP__":
                    stopping = True
                    break
                self._apply(msg)
        except queue.Empty:
            pass
        if stopping:
            self._poll_job = None
            try:
                self._tk.quit()  # mainloop returns -> finally cleans up
            except Exception:
                pass
            return
        self._poll_job = self._tk.after(40, self._poll_queue)

    # --- drawing ----------------------------------------------------------

    def _draw_pill_body(self) -> None:
        """Solid rounded-rect via two end caps + a center rectangle."""
        c = self._canvas
        w, h = self._w, self._h
        r = h // 2
        c.create_oval(0, 0, 2 * r, h, fill=_BG, outline="")
        c.create_oval(w - 2 * r, 0, w, h, fill=_BG, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=_BG, outline="")
        # Top edge highlight — purely cosmetic so the pill reads as a surface.
        c.create_arc(0, 0, 2 * r, h, start=30, extent=120, style="arc", outline=_BG_EDGE)
        c.create_arc(w - 2 * r, 0, w, h, start=30, extent=120, style="arc", outline=_BG_EDGE)
        c.create_line(r, 0, w - r, 0, fill=_BG_EDGE)

    def _redraw(self) -> None:
        if self._canvas is None:
            return
        c = self._canvas
        c.delete("all")
        self._draw_pill_body()

        if self._state not in _PALETTE:
            return
        label, accent = _PALETTE[self._state]
        icon_cx = self._h // 2 + 2
        icon_cy = self._h // 2

        if self._state == "recording":
            self._draw_bars(icon_cx, icon_cy, accent)
        elif self._state == "transcribing":
            self._draw_spinner(icon_cx, icon_cy, accent)
        elif self._state == "polishing":
            self._draw_dots(icon_cx, icon_cy, accent)

        c.create_text(
            self._h + 4,
            icon_cy + 1,
            text=label,
            fill=_FG,
            font=("Segoe UI", 9),
            anchor="w",
        )

    def _draw_bars(self, cx: int, cy: int, color: str) -> None:
        """3-bar pulsing waveform; heights modulated by sine(tick)."""
        c = self._canvas
        bar_w, gap, bars = 2, 2, 3
        total = bars * bar_w + (bars - 1) * gap
        x0 = cx - total // 2
        max_h = self._h - 12
        for i in range(bars):
            phase = self._tick / 3.5 + i * 0.7
            amp = (math.sin(phase) + 1) / 2  # 0..1
            bh = max(3, int(max_h * (0.3 + 0.7 * amp)))
            x = x0 + i * (bar_w + gap)
            y0 = cy - bh // 2
            c.create_rectangle(x, y0, x + bar_w, y0 + bh, fill=color, outline="")

    def _draw_spinner(self, cx: int, cy: int, color: str) -> None:
        """iOS-style spinner: 8 radial spokes with a fading tail. The 'head'
        spoke advances around the circle every 2 frames (~8 fps rotation)."""
        c = self._canvas
        n = 8
        head = (self._tick // 2) % n
        outer_r = (self._h - 12) // 2
        inner_r = max(2, outer_r - 4)
        for i in range(n):
            d = (head - i) % n             # spokes back from the head
            t = d / (n - 1)                # 0 (brightest) .. 1 (dimmest)
            spoke = _blend(color, _BG, 0.15 + 0.85 * (t ** 1.3))
            ang = math.radians(i * (360 / n) - 90)
            cosA, sinA = math.cos(ang), math.sin(ang)
            x0 = cx + inner_r * cosA
            y0 = cy + inner_r * sinA
            x1 = cx + outer_r * cosA
            y1 = cy + outer_r * sinA
            c.create_line(x0, y0, x1, y1, fill=spoke, width=2, capstyle="round")

    def _draw_dots(self, cx: int, cy: int, color: str) -> None:
        """Three dots pulsing in sequence — the universal 'thinking/refining'
        cue. Each dot follows the same sine curve, offset by a phase shift,
        and breathes between ~22% and 100% accent."""
        c = self._canvas
        spacing = 6
        base_r = 1.8
        x_start = cx - spacing
        for i in range(3):
            phase = self._tick / 4.5 - i * 1.1
            amp = (math.sin(phase) + 1) / 2          # 0..1
            ease = amp * amp * (3 - 2 * amp)         # smoothstep — softer
            col = _blend(color, _BG, 1 - (0.22 + 0.78 * ease))
            r = base_r + 0.7 * ease                  # subtle size pulse
            dx = x_start + i * spacing
            c.create_oval(
                dx - r, cy - r, dx + r, cy + r,
                fill=col, outline="",
            )

    def _animate(self) -> None:
        if self._tk is None or self._state == "idle":
            self._anim_job = None
            return
        self._tick += 1
        self._redraw()
        self._anim_job = self._tk.after(60, self._animate)

    # --- public API -------------------------------------------------------

    def set_state(self, state: str) -> None:
        if self._beep:
            self._play(state)
        # Queue the message instead of touching Tcl directly — `tk.after()`
        # is not actually thread-safe on Windows.
        self._queue.put(state)

    def _apply(self, state: str) -> None:
        try:
            self._state = state
            if state == "idle":
                self._tk.withdraw()
                if self._anim_job is not None:
                    try:
                        self._tk.after_cancel(self._anim_job)
                    except Exception:
                        pass
                    self._anim_job = None
                return
            self._tick = 0
            self._redraw()
            self._tk.deiconify()
            self._tk.lift()
            self._tk.attributes("-topmost", True)
            if self._anim_job is None:
                self._animate()
        except Exception:
            pass

    def _play(self, state: str) -> None:
        try:
            import winsound  # noqa: PLC0415  — Windows-only
        except Exception:
            return
        tones = {
            "recording":    (880, 80),
            "transcribing": (660, 50),
            "polishing":    (740, 50),
            "idle":         (520, 80),
        }
        params = tones.get(state)
        if params is None:
            return
        threading.Thread(target=winsound.Beep, args=params, daemon=True).start()

    def stop(self) -> None:
        # The poller picks up __STOP__, calls quit() on the Tk thread, and
        # the thread's finally-block destroys the interpreter from the same
        # thread that created it.
        self._queue.put("__STOP__")
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
