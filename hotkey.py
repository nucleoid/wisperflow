"""Windows push-to-talk hotkey with system-level event suppression.

pynput's `GlobalHotKeys` doesn't consume the chord — the keystroke still
reaches the focused window. For Ctrl+Alt+Space that means:
  - PowerShell / Windows Terminal sees the Space and inserts spaces at
    the prompt (autorepeat makes it worse the longer you hold).
  - mintty (Git Bash) passes the chord to bash readline, which rings the
    bell on the unrecognized binding; mintty's visual bell flashes the
    window white.

This module installs a low-level keyboard hook via pynput's
`win32_event_filter` and suppresses just the action key (e.g. Space)
while the modifier set (Ctrl+Alt) is held. Modifier keys themselves
pass through normally, so other Ctrl/Alt shortcuts in the focused app
keep working.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

# Virtual key codes (Windows).
_VK_LBUTTON = 0x01
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12        # Alt
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_LCONTROL = 0xA2
_VK_RCONTROL = 0xA3
_VK_LMENU = 0xA4
_VK_RMENU = 0xA5
_VK_LSHIFT = 0xA0
_VK_RSHIFT = 0xA1

_VK_SPACE = 0x20
_VK_RETURN = 0x0D
_VK_TAB = 0x09
_VK_ESCAPE = 0x1B
_VK_BACK = 0x08
_VK_DELETE = 0x2E

# WM_* messages we care about.
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105

# KBDLLHOOKSTRUCT flag — set on events we synthesised (via SendInput, etc.).
_LLKHF_INJECTED = 0x10

_MODIFIER_TOKENS = {
    "<ctrl>":    _VK_CONTROL,
    "<ctrl_l>":  _VK_CONTROL,
    "<ctrl_r>":  _VK_CONTROL,
    "<alt>":     _VK_MENU,
    "<alt_l>":   _VK_MENU,
    "<alt_r>":   _VK_MENU,
    "<shift>":   _VK_SHIFT,
    "<shift_l>": _VK_SHIFT,
    "<shift_r>": _VK_SHIFT,
    "<cmd>":     _VK_LWIN,
    "<win>":     _VK_LWIN,
}

_NAMED_KEY_TOKENS = {
    "<space>":     _VK_SPACE,
    "<enter>":     _VK_RETURN,
    "<return>":    _VK_RETURN,
    "<tab>":       _VK_TAB,
    "<esc>":       _VK_ESCAPE,
    "<escape>":    _VK_ESCAPE,
    "<backspace>": _VK_BACK,
    "<delete>":    _VK_DELETE,
    **{f"<f{i}>": 0x70 + (i - 1) for i in range(1, 13)},
}


def parse_hotkey(spec: str) -> tuple[set[int], int] | None:
    """Parse a pynput hotkey string into (modifier VKs, action VK).

    Returns None if the string can't be expressed as a single action key
    plus a set of modifiers — falls back to no-suppression in that case.
    """
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    mods: set[int] = set()
    action: int | None = None
    for p in parts:
        if p in _MODIFIER_TOKENS:
            mods.add(_MODIFIER_TOKENS[p])
            continue
        vk = _NAMED_KEY_TOKENS.get(p)
        if vk is None and len(p) == 1:
            ch = p.upper()
            if "A" <= ch <= "Z" or "0" <= ch <= "9":
                vk = ord(ch)
        if vk is None:
            return None
        if action is not None:
            return None  # only one action key supported
        action = vk
    if action is None or not mods:
        return None
    return mods, action


class WindowsPushToTalk:
    """Push-to-talk hotkey for Windows that suppresses the action key.

    `on_press` fires once when the chord first goes down. `on_release`
    fires when the chord is broken (action key up, or any required
    modifier released while the chord is active). The action key (and
    only the action key) is suppressed system-wide so the chord never
    reaches the focused app. Modifier key events pass through normally.
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsPushToTalk only supports Windows")
        parsed = parse_hotkey(hotkey)
        if parsed is None:
            raise ValueError(f"unsupported hotkey for win32 suppression: {hotkey!r}")

        from pynput import keyboard  # noqa: PLC0415
        import ctypes  # noqa: PLC0415

        self._mods, self._action = parsed
        self._on_press_cb = on_press
        self._on_release_cb = on_release
        self._user32 = ctypes.windll.user32

        self._lock = threading.Lock()
        self._active = False
        self._suppress_action_up = False

        self._listener = keyboard.Listener(win32_event_filter=self._filter)

    # --- helpers ----------------------------------------------------------

    def _held(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _all_mods_held(self) -> bool:
        return all(self._held(vk) for vk in self._mods)

    @staticmethod
    def _spawn(fn: Callable[[], None]) -> None:
        def runner() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=runner, daemon=True).start()

    # --- hook filter ------------------------------------------------------

    def _filter(self, msg, data):
        # Don't react to events we ourselves injected (e.g. the injector's
        # Ctrl+V or unicode keystrokes for terminal type-mode).
        if data.flags & _LLKHF_INJECTED:
            return True

        is_down = msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
        is_up = msg in (_WM_KEYUP, _WM_SYSKEYUP)
        vk = data.vkCode

        # Normalise side-specific modifier VKs to their generic equivalents
        # so a chord defined as <ctrl> matches both LCONTROL and RCONTROL.
        norm = vk
        if vk in (_VK_LCONTROL, _VK_RCONTROL):
            norm = _VK_CONTROL
        elif vk in (_VK_LMENU, _VK_RMENU):
            norm = _VK_MENU
        elif vk in (_VK_LSHIFT, _VK_RSHIFT):
            norm = _VK_SHIFT

        with self._lock:
            if vk == self._action:
                if is_down and self._all_mods_held():
                    if not self._active:
                        self._active = True
                        self._spawn(self._on_press_cb)
                    self._suppress_action_up = True
                    # Raises SuppressException — propagates up to the hook,
                    # which returns 1 to Windows so the focused app never
                    # sees this key event.
                    self._listener.suppress_event()
                if is_up and self._suppress_action_up:
                    self._suppress_action_up = False
                    if self._active:
                        self._active = False
                        self._spawn(self._on_release_cb)
                    self._listener.suppress_event()
                return True

            # Releasing one of the chord's modifiers ends the press without
            # suppressing the modifier itself (we want apps to see the
            # modifier go up).
            if is_up and self._active and norm in self._mods:
                self._active = False
                self._spawn(self._on_release_cb)

        return True

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        try:
            self._listener.stop()
        except Exception:  # noqa: BLE001
            pass
