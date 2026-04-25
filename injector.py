"""Inject text into whatever app currently has focus.

Default strategy on GUI apps: save clipboard, set clipboard, send Ctrl+V,
restore clipboard. In terminals, Ctrl+V often does something other than paste
(in mintty it's readline's quoted-insert; in legacy conhost it's nothing), so
we auto-switch to unicode keystroke injection there.

We also wait for the hotkey's own keys to be physically released before
injecting — otherwise Alt (from Ctrl+Alt+Space) leaks into the Ctrl+V and
gets interpreted as Ctrl+Alt+V, which produces garbage in some terminals.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pyperclip
from pynput.keyboard import Controller, Key

from config import CONFIG

_kb = Controller()

# Processes we treat as terminals. When one of these is in the foreground, we
# type characters instead of pressing Ctrl+V.
_TERMINAL_EXES = {
    "windowsterminal.exe",
    "openconsole.exe",  # Windows Terminal's conpty host
    "conhost.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "mintty.exe",
    "wsltty.exe",
    "alacritty.exe",
    "wezterm-gui.exe",
    "wezterm.exe",
    "tabby.exe",
    "kitty.exe",
    "xterm.exe",
}


def _wait_keys_released(timeout: float = 1.0) -> None:
    """Block until Ctrl, Alt, Shift, Win, and Space are all up (Windows only).

    Without this, the hotkey release can fire while the user is still holding
    Alt, so our subsequent Ctrl+V becomes Ctrl+Alt+V (terminal garbage) and
    any typed characters pick up stray modifiers.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415
        user32 = ctypes.windll.user32
    except Exception:
        return
    # VK_CONTROL, VK_MENU (Alt), VK_SHIFT, VK_LWIN, VK_RWIN, VK_SPACE
    vks = (0x11, 0x12, 0x10, 0x5B, 0x5C, 0x20)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = False
        for vk in vks:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                held = True
                break
        if not held:
            return
        time.sleep(0.01)


def _foreground_exe() -> str | None:
    """Return the foreground window's process image basename, lowercased."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # PROCESS_QUERY_LIMITED_INFORMATION — works even for elevated processes.
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None
    return None


def _focus_is_terminal() -> bool:
    exe = _foreground_exe()
    return exe is not None and exe in _TERMINAL_EXES


def _send_paste_pynput() -> None:
    mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with _kb.pressed(mod):
        _kb.press("v")
        _kb.release("v")


def _send_paste_xdotool() -> bool:
    """Prefer xdotool on Linux — more reliable across toolkits."""
    if sys.platform != "linux" or not CONFIG.linux_use_xdotool:
        return False
    if not shutil.which("xdotool"):
        return False
    try:
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
            check=True,
            timeout=2,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _type_text(text: str) -> None:
    # pynput's .type() on Windows uses KEYEVENTF_UNICODE, which is accepted by
    # every terminal (mintty, conhost, Windows Terminal, etc.) as real input.
    for ch in text:
        _kb.type(ch)


def _choose_mode() -> str:
    """Return "paste" or "type"."""
    mode = (CONFIG.inject_mode or "paste").lower()
    if mode == "type":
        return "type"
    if mode == "paste":
        return "paste"
    # "auto" — type in terminals, paste elsewhere.
    return "type" if _focus_is_terminal() else "paste"


def inject(text: str) -> None:
    if not text:
        return

    _wait_keys_released()
    mode = _choose_mode()

    if mode == "type":
        _type_text(text)
        return

    # Save/restore clipboard so we don't clobber what the user had copied.
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        previous = None

    pyperclip.copy(text)
    # Give the clipboard manager a moment to settle on some Linux setups.
    time.sleep(0.03)

    if not _send_paste_xdotool():
        _send_paste_pynput()

    if previous is not None:
        # Restore after the paste completes in the target app.
        time.sleep(0.15)
        try:
            pyperclip.copy(previous)
        except pyperclip.PyperclipException:
            pass
