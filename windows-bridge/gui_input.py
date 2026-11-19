"""Keyboard input and matching helpers for the OpenBot GUI bridge.

Pure logic (name matching, hotkey parsing, window picking, UTF-16 splitting) is
platform-independent and unit-tested on Linux; only `send_*` touch Win32 SendInput.
"""
import sys
import time
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Name matching
# --------------------------------------------------------------------------

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', " ": " "})


def norm_name(s: Optional[str]) -> str:
    """Lowercase, straight quotes, collapsed whitespace, '&' accelerators dropped."""
    s = (s or "").translate(_QUOTES).replace("&", "")
    return " ".join(s.split()).lower()


def pick_by_name(candidates: Sequence[Tuple[str, str]], name: str, role: Optional[str] = None) -> Optional[int]:
    """candidates: [(name, role)] in tree order. Returns index of best match or None.

    Exact (normalised) name beats substring; empty names never match; role filter is strict.
    """
    needle = norm_name(name)
    if not needle:
        return None
    want_role = norm_name(role).replace("control", "") if role else None
    partial = None
    for i, (n, r) in enumerate(candidates):
        nn = norm_name(n)
        if not nn:
            continue
        if want_role and norm_name(r).replace("control", "") != want_role:
            continue
        if nn == needle:
            return i
        if partial is None and needle in nn:
            partial = i
    return partial


def clean_text(s: Optional[str]) -> str:
    """Replace lone UTF-16 surrogates (broken emoji from UI controls) so JSON encoding never fails."""
    if not s:
        return ""
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


# --------------------------------------------------------------------------
# Window picking for browser text
# --------------------------------------------------------------------------

BROWSER_SUFFIXES = (" - google chrome", " - microsoft edge", " - brave")


def is_browser_title(title: str) -> bool:
    t = (title or "").lower().strip()
    return any(t.endswith(sfx) for sfx in BROWSER_SUFFIXES)


def pick_browser_window(titles: Sequence[str], tab_match: str = "") -> Optional[int]:
    """titles in z-order (topmost first). Only real browser windows qualify (not Electron apps)."""
    m = (tab_match or "").lower()
    for i, t in enumerate(titles):
        if is_browser_title(t) and m in t.lower():
            return i
    return None


# --------------------------------------------------------------------------
# Hotkeys
# --------------------------------------------------------------------------

VK = {
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0x12, "menu": 0x12, "lalt": 0xA4, "ralt": 0xA5, "altgr": 0xA5,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "super": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "printscreen": 0x2C, "prtsc": 0x2C, "apps": 0x5D, "menukey": 0x5D,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i
EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5C, 0x5D, 0xA3, 0xA5, 0x2C}


def key_to_vk(name: str) -> int:
    k = name.strip().lower()
    if k in VK:
        return VK[k]
    if len(k) == 1 and (k.isalnum()):
        return ord(k.upper())
    raise ValueError(f"unknown key {name!r}")


def parse_hotkey(key: Optional[str] = None, keys: Optional[Sequence[str]] = None) -> List[int]:
    if keys:
        parts = list(keys)
    elif key:
        parts = [key] if key.strip() == "+" else [p for p in key.split("+") if p.strip()]
    else:
        raise ValueError("missing key")
    return [key_to_vk(p) for p in parts]


# --------------------------------------------------------------------------
# Text -> UTF-16 code units (SendInput KEYEVENTF_UNICODE)
# --------------------------------------------------------------------------

# Win11 Notepad/XAML (TSF) merges Unicode key events sent faster than ~15 ms apart
# (measured 2026-11-19: 0.01 s -> corrupted, 0.015/0.02 s -> exact). Never type faster.
MIN_TYPE_INTERVAL = 0.015
DEFAULT_TYPE_INTERVAL = 0.02


def type_interval(requested: Optional[float]) -> float:
    if not requested:
        return DEFAULT_TYPE_INTERVAL
    return max(float(requested), MIN_TYPE_INTERVAL)


def utf16_units(text: str) -> List[int]:
    data = text.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


# --------------------------------------------------------------------------
# Win32 SendInput
# --------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_SCANCODE = 0x0008
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)

    def _send(inputs: List["INPUT"]) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        sent = _user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise OSError(f"SendInput sent {sent}/{len(inputs)} (err {ctypes.get_last_error()}); "
                          "input may be blocked by a higher-integrity window or the secure desktop")

    def _vk_input(vk: int, up: bool) -> "INPUT":
        scan = _user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0))

    def send_hotkey(vks: List[int], hold: float = 0.03) -> None:
        """Press keys in order, release in reverse, with small gaps (Alt combos need them)."""
        for vk in vks:
            _send([_vk_input(vk, False)])
            time.sleep(hold)
        for vk in reversed(vks):
            _send([_vk_input(vk, True)])
            time.sleep(0.01)

    def send_text(text: str, interval: Optional[float] = None) -> None:
        """Type any text layout-independently: \\n/\\t as keys, everything else as Unicode units."""
        interval = type_interval(interval)
        for ch in text:
            if ch == "\n":
                send_hotkey([0x0D], hold=0.005)
            elif ch == "\r":
                continue
            elif ch == "\t":
                send_hotkey([0x09], hold=0.005)
            else:
                batch = []
                for unit in utf16_units(ch):
                    batch.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                        wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)))
                    batch.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                        wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)))
                _send(batch)
            time.sleep(interval)
