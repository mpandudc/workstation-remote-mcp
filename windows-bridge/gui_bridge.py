"""OpenBot Windows GUI Bridge v2.

Runs as pythonw.exe in the interactive session (Session 1) via scheduled task
`OpenBot-GUI-Bridge`. Every endpoint requires the `x-token` header.

v2 adds: JPEG + region screenshots with coordinate metadata, batched steps,
UI Automation (UIA) tree/click/set-text, and page text via Chrome CDP
(127.0.0.1 only) with a UIA fallback.
"""
import base64
import hmac
import concurrent.futures
import ctypes
import io
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import mss

_MSS = getattr(mss, "MSS", None) or mss.mss
import pyautogui
import uvicorn
import win32gui

import gui_input as gi
from fastapi import FastAPI, Header, HTTPException, Query, Response
from PIL import Image
from pydantic import BaseModel

# Per-monitor DPI awareness: screenshot pixels == click coordinates == UIA rects.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_token() -> str:
    """GUI_BRIDGE_TOKEN from the environment, else from .env beside this script. No default."""
    tok = (os.environ.get("GUI_BRIDGE_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key.strip() == "GUI_BRIDGE_TOKEN":
                        return val.strip().strip("'\"")
    except OSError:
        pass
    return ""


AUTH_TOKEN = _load_token()
CDP_URL = "http://127.0.0.1:9222"  # never exposed; only this process talks to it
DEFAULT_MAX_BYTES = 150_000

app = FastAPI(title="OpenBot Windows GUI Bridge", version="2.1.0")


def check_auth(x_token: Optional[str]) -> None:
    if not AUTH_TOKEN or not x_token or not hmac.compare_digest(x_token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


# --------------------------------------------------------------------------
# Screenshot
# --------------------------------------------------------------------------

def parse_region(region: Any) -> Optional[Tuple[int, int, int, int]]:
    """'x0,y0,x1,y1' or [x0,y0,x1,y1] -> tuple; None for full screen."""
    if region in (None, "", []):
        return None
    if isinstance(region, str):
        parts = [p for p in region.replace(" ", "").split(",") if p]
    else:
        parts = list(region)
    if len(parts) != 4:
        raise ValueError("region must be x0,y0,x1,y1")
    x0, y0, x1, y1 = (int(float(p)) for p in parts)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("region needs x1>x0 and y1>y0")
    return x0, y0, x1, y1


def capture(region=None, max_width: Optional[int] = 1600, fmt: str = "jpeg",
            quality: int = 70, max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> Tuple[bytes, Dict[str, Any]]:
    t0 = time.perf_counter()
    reg = parse_region(region)
    with _MSS() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        if reg:
            x0 = max(reg[0], mon["left"]); y0 = max(reg[1], mon["top"])
            x1 = min(reg[2], mon["left"] + mon["width"]); y1 = min(reg[3], mon["top"] + mon["height"])
            if x1 <= x0 or y1 <= y0:
                raise ValueError("region is outside the primary monitor")
            box = {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}
        else:
            box = {k: mon[k] for k in ("left", "top", "width", "height")}
        shot = sct.grab(box)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    src_w, src_h = img.size
    fmt = (fmt or "jpeg").lower()
    is_png = fmt == "png"
    width = min(max_width, src_w) if max_width else src_w
    q = max(20, min(int(quality), 95))
    attempts = 0
    while True:
        attempts += 1
        out = img if width == src_w else img.resize(
            (width, max(1, round(src_h * width / src_w))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        if is_png:
            out.save(buf, format="PNG", optimize=False)
        else:
            out.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        # Size budget: lower JPEG quality first (down to 40), then width. PNG is never squeezed.
        if is_png or not max_bytes or len(data) <= max_bytes or attempts >= 8:
            break
        if q > 40:
            q = max(40, q - 15)
        else:
            width = max(400, int(width * 0.8))
    meta = {
        "format": "png" if is_png else "jpeg",
        "bytes": len(data),
        "width": out.size[0],
        "height": out.size[1],
        "quality": None if is_png else q,
        "origin": [box["left"], box["top"]],
        "source_size": [src_w, src_h],
        # screen_x = origin[0] + image_x * scale ; same for y
        "scale": round(src_w / out.size[0], 6),
        "capture_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return data, meta


# --------------------------------------------------------------------------
# UI Automation (all UIA calls run on one dedicated COM thread)
# --------------------------------------------------------------------------

def _uia_init():
    # comtypes (imported by uiautomation) initialises COM on this thread itself;
    # calling CoInitializeEx first makes it fail with "cannot change thread mode".
    import uiautomation  # noqa: F401


_UIA = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_uia_init)
_CACHE: Dict[str, Any] = {}
_CACHE_MAX = 5000


def on_uia(fn, *args, timeout: float = 25.0, **kwargs):
    return _UIA.submit(fn, *args, **kwargs).result(timeout=timeout)


def _role(c) -> str:
    return (c.ControlTypeName or "").replace("Control", "")


def _rid(c) -> str:
    try:
        rid = c.GetRuntimeId()
        if rid:
            return "e" + ".".join(str(x) for x in rid)
    except Exception:
        pass
    return "h%x" % (c.NativeWindowHandle or id(c))


def _rect(c) -> Optional[List[int]]:
    try:
        r = c.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return None
        return [r.left, r.top, r.right, r.bottom]
    except Exception:
        return None


def _remember(c) -> str:
    key = _rid(c)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = c
    return key


def _top_window(window_title: Optional[str]):
    import uiautomation as uia
    if not window_title:
        fg = uia.GetForegroundControl()
        if fg is None:
            raise LookupError("no foreground window")
        return fg.GetTopLevelControl() or fg
    needle = window_title.lower()
    for w in uia.GetRootControl().GetChildren():
        if needle in (w.Name or "").lower():
            return w
    raise LookupError(f"no top-level window title contains {window_title!r}")


def _walk(root, max_depth: int, max_nodes: int, deadline: float):
    stack = [(root, 0)]
    seen = 0
    while stack:
        c, d = stack.pop()
        yield c, d
        seen += 1
        if seen >= max_nodes or time.perf_counter() > deadline:
            return
        if d >= max_depth:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            stack.append((k, d + 1))


def uia_tree(window_title=None, query=None, max_depth=12, limit=150, include_unnamed=False,
             max_nodes=4000, time_budget=8.0) -> Dict[str, Any]:
    t0 = time.perf_counter()
    win = _top_window(window_title)
    q = (query or "").lower()
    out = []
    visited = 0
    skipped = 0
    for c, depth in _walk(win, max_depth, max_nodes, t0 + time_budget):
        visited += 1
        try:  # nodes can vanish mid-walk (COMError 0x80040201); skip them, keep the rest
            name = c.Name or ""
            aid = c.AutomationId or ""
            role = _role(c)
        except Exception:
            skipped += 1
            continue
        if not (name or aid or include_unnamed or role in ("Edit", "Document")):
            continue
        if q and q not in name.lower() and q not in aid.lower() and q != role.lower():
            continue
        rect = _rect(c)
        if rect is None and depth > 0:
            continue
        item = {"id": _remember(c), "role": role, "name": gi.clean_text(name[:200]), "rect": rect, "depth": depth}
        if aid:
            item["automation_id"] = aid
        try:
            if not c.IsEnabled:
                item["enabled"] = False
        except Exception:
            pass
        out.append(item)
        if len(out) >= limit:
            break
    return {
        "status": "ok",
        "window": {"title": gi.clean_text(win.Name), "rect": _rect(win), "id": _remember(win)},
        "elements": out,
        "count": len(out),
        "visited": visited,
        "skipped": skipped,
        "truncated": len(out) >= limit or visited >= max_nodes,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _resolve(id=None, name=None, role=None, window_title=None):
    if id:
        c = _CACHE.get(id)
        if c is None:
            raise LookupError(f"unknown element id {id!r}; call pc_ui_tree again")
        return c
    if not name:
        raise ValueError("give id or name")
    if not gi.norm_name(name):
        raise ValueError("name is empty")
    win = _top_window(window_title)
    ctrls, cands = [], []
    for c, _ in _walk(win, 25, 6000, time.perf_counter() + 8):
        try:
            n, r = c.Name or "", _role(c)
        except Exception:
            continue
        if not n or not _rect(c):
            continue
        ctrls.append(c)
        cands.append((n, r))
    i = gi.pick_by_name(cands, name, role)
    if i is None:
        raise LookupError(f"no element named {name!r}" + (f" with role {role}" if role else "")
                          + f" in window {win.Name!r}")
    c = ctrls[i]
    _remember(c)
    return c


def _center(c):
    r = _rect(c)
    if not r:
        raise LookupError("element has no on-screen rect")
    return (r[0] + r[2]) // 2, (r[1] + r[3]) // 2


def _info(c):
    return {"id": _rid(c), "name": gi.clean_text(c.Name), "role": _role(c), "rect": _rect(c)}


def uia_click(id=None, name=None, role=None, window_title=None, method="auto", button="left", clicks=1):
    c = _resolve(id, name, role, window_title)
    before = _info(c)
    used = None
    if method in ("auto", "invoke") and button == "left" and clicks == 1:
        for getter, act, label in (
            ("GetInvokePattern", lambda p: p.Invoke(), "invoke"),
            ("GetTogglePattern", lambda p: p.Toggle(), "toggle"),
            ("GetSelectionItemPattern", lambda p: p.Select(), "select"),
            ("GetExpandCollapsePattern", lambda p: p.Expand(), "expand"),
        ):
            try:
                p = getattr(c, getter)()
                if p:
                    act(p)
                    used = label
                    break
            except Exception:
                continue
        if used is None and method == "invoke":
            raise RuntimeError("element supports no invoke/toggle/select pattern; use method=mouse")
    if used is None:
        x, y = _center(c)
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        used = "mouse"
    return {"status": "ok", **before, "method": used}


def uia_set_text(text: str, id=None, name=None, role=None, window_title=None, append=False):
    c = _resolve(id, name, role, window_title)
    used = None
    if not append:
        try:
            vp = c.GetValuePattern()
            if vp and not vp.IsReadOnly:
                vp.SetValue(text)
                used = "value_pattern"
        except Exception:
            used = None
    if used is None:
        try:
            c.SetFocus()
        except Exception:
            x, y = _center(c)
            pyautogui.click(x=x, y=y)
        time.sleep(0.05)
        if not append:
            gi.send_hotkey([0x11, ord("A")])
        _type(text)
        used = "keyboard"
    value = None
    try:
        vp = c.GetValuePattern()
        value = vp.Value if vp else None
    except Exception:
        pass
    return {"status": "ok", "id": _rid(c), "name": c.Name, "method": used, "value": value}


def uia_get_text(id=None, name=None, role=None, window_title=None, max_chars=200000):
    c = _resolve(id, name, role, window_title)
    text, src = None, None
    try:
        tp = c.GetTextPattern()
        if tp:
            text, src = tp.DocumentRange.GetText(max_chars), "text_pattern"
    except Exception:
        pass
    if text is None:
        try:
            vp = c.GetValuePattern()
            if vp:
                text, src = vp.Value, "value_pattern"
        except Exception:
            pass
    if text is None:
        text, src = c.Name or "", "name"
    text = gi.clean_text(text)
    return {"status": "ok", **_info(c), "source": src, "length": len(text), "text": text[:max_chars]}


def uia_select_text(text: str, id=None, name=None, role=None, window_title=None):
    """Select the first exact occurrence of `text` in a text control (TextPattern)."""
    if not text:
        raise ValueError("text is empty")
    c = _resolve(id, name, role, window_title)
    tp = c.GetTextPattern()
    if not tp:
        raise LookupError("element has no TextPattern")
    full = tp.DocumentRange.GetText(-1)
    count = full.count(text)
    if count == 0:
        raise LookupError("text not found in element")
    rng = tp.DocumentRange.FindText(text, False, False)
    if rng is None:
        raise LookupError("FindText returned nothing")
    rng.Select()
    sel = tp.GetSelection()
    got = sel[0].GetText(-1) if sel else None
    if got != text:
        raise RuntimeError(f"selection mismatch ({len(got or '')} chars selected)")
    return {"status": "ok", **_info(c), "selected_chars": len(text), "occurrences": count}


def _focus_window(window_title: str):
    w = _top_window(window_title)
    w.SetActive()
    return {"title": w.Name}


# --------------------------------------------------------------------------
# Browser text: CDP on 127.0.0.1:9222 if Chrome was started with it, else UIA
# --------------------------------------------------------------------------

_PAGE_JS = r"""
(() => {
  const maxc = __MAXC__;
  function grab(win, depth) {
    const r = {url: '', title: '', text: '', terminal: null, frames: []};
    try { r.url = win.location.href; r.title = win.document.title; } catch (e) { return null; }
    let t = null;
    try { t = win.term || win.terminal || null; } catch (e) {}
    if (t && t.buffer && t.buffer.active) {
      const b = t.buffer.active, lines = [];
      for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) lines.push(l.translateToString(true)); }
      while (lines.length && !lines[lines.length - 1]) lines.pop();
      r.terminal = lines.slice(-2000).join('\n');
    } else {
      const rows = win.document.querySelector('.xterm-accessibility-tree, .xterm-rows');
      if (rows && rows.innerText.trim()) r.terminal = rows.innerText;
    }
    try { r.text = (win.document.body ? win.document.body.innerText : '').slice(0, maxc); } catch (e) {}
    if (depth < 3) {
      for (const f of win.document.querySelectorAll('iframe, frame')) {
        try { const s = grab(f.contentWindow, depth + 1); if (s) r.frames.push(s); } catch (e) {}
      }
    }
    return r;
  }
  return grab(window, 0);
})()
"""


def _cdp_targets() -> Optional[List[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(CDP_URL + "/json/list", timeout=1.0) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _cdp_eval(ws_url: str, expr: str, timeout: float = 10.0):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(str(res["exceptionDetails"])[:300])
                return res.get("result", {}).get("value")
    finally:
        ws.close()


def browser_text(tab_match: str = "", max_chars: int = 20000) -> Dict[str, Any]:
    targets = _cdp_targets()
    if targets is not None:
        pages = [t for t in targets if t.get("type") == "page"]
        m = (tab_match or "").lower()
        hit = [t for t in pages if m in (t.get("title", "") + " " + t.get("url", "")).lower()]
        if not hit:
            return {"status": "error", "backend": "cdp", "error": f"no tab matches {tab_match!r}",
                    "tabs": [{"title": t.get("title"), "url": t.get("url")} for t in pages][:30]}
        t = hit[0]
        val = _cdp_eval(t["webSocketDebuggerUrl"], _PAGE_JS.replace("__MAXC__", str(int(max_chars))))

        def flat(node):
            yield node
            for f in node.get("frames") or []:
                yield from flat(f)
        nodes = list(flat(val or {}))
        terminal = next((n["terminal"] for n in nodes if n.get("terminal")), None)
        text = "\n\n".join(n.get("text", "") for n in nodes if n.get("text"))[:max_chars]
        return {"status": "ok", "backend": "cdp", "title": t.get("title"), "url": t.get("url"),
                "terminal": terminal[-max_chars:] if terminal else None, "text": text,
                "other_matches": len(hit) - 1}
    return on_uia(_uia_browser_text, tab_match, max_chars)


def _uia_browser_text(tab_match: str, max_chars: int) -> Dict[str, Any]:
    """Fallback without CDP: only the *active* tab of a Chrome window is readable."""
    import uiautomation as uia
    wins = [w for w in uia.GetRootControl().GetChildren()
            if (w.ClassName or "").startswith("Chrome_WidgetWin") and (w.Name or "").strip()]
    idx = gi.pick_browser_window([w.Name for w in wins], tab_match)  # root children are z-ordered
    win = wins[idx] if idx is not None else None
    if win is None:
        return {"status": "error", "backend": "uia",
                "error": f"no Chrome/Edge window whose active tab title contains {tab_match!r} "
                         "(UIA reads only the active tab; start Chrome with CDP for background tabs)",
                "windows": [w.Name for w in wins][:20]}
    # Pre-order walk; only text below a Document (web content) counts, not the tab strip/toolbar.
    textish = {"Text", "Hyperlink", "Header", "HeaderItem", "DataItem", "ListItem", "TreeItem", "Edit"}
    parts: List[str] = []
    total = docs = visited = 0
    last = None
    stack = [(win, 0, False)]
    deadline = time.perf_counter() + 12
    while stack and visited < 25000 and time.perf_counter() < deadline:
        c, d, in_doc = stack.pop()
        visited += 1
        try:
            role = _role(c)
        except Exception:
            continue
        if role == "Document":
            docs += 1
            in_doc = True
        elif in_doc and role in textish:
            try:
                n = (c.Name or "").strip()
            except Exception:
                n = ""
            if n and n != last and any(ch.isalnum() for ch in n):
                parts.append(n)
                last = n
                total += len(n) + 1
                if total >= max_chars:
                    break
        if d < 80:
            try:
                kids = c.GetChildren()
            except Exception:
                kids = []
            for k in reversed(kids):
                stack.append((k, d + 1, in_doc))
    return {"status": "ok", "backend": "uia", "title": win.Name, "url": None, "terminal": None,
            "text": gi.clean_text("\n".join(parts)[:max_chars]), "documents": docs, "visited": visited,
            "note": "UIA fallback: active tab only; canvas terminals (xterm.js) need CDP"}


# --------------------------------------------------------------------------
# Actions shared by single endpoints and /steps
# --------------------------------------------------------------------------

def _press(key: Optional[str] = None, keys: Optional[List[str]] = None):
    vks = gi.parse_hotkey(key, keys)
    gi.send_hotkey(vks)
    return {"keys": list(keys) if keys else key, "vk": vks}


def _type(text: str, interval: float = 0.0):
    # SendInput KEYEVENTF_UNICODE: layout-independent, keeps capitals/symbols, clipboard untouched.
    gi.send_text(text, interval)
    return {"chars": len(text)}


def _active_title() -> str:
    hwnd = win32gui.GetForegroundWindow()
    return win32gui.GetWindowText(hwnd) if hwnd else ""


def do_action(step: Dict[str, Any]) -> Dict[str, Any]:
    a = (step.get("action") or "").lower()
    # Guard against typing into the wrong window (the PC is shared with its owner).
    expect = step.get("expect_window")
    if expect and expect.lower() not in _active_title().lower():
        raise RuntimeError(f"active window is {_active_title()!r}, expected {expect!r}")
    if a == "click":
        pyautogui.click(x=step["x"], y=step["y"], button=step.get("button", "left"), clicks=step.get("clicks", 1))
        return {"x": step["x"], "y": step["y"]}
    if a == "double_click":
        pyautogui.click(x=step["x"], y=step["y"], clicks=2)
        return {"x": step["x"], "y": step["y"]}
    if a == "right_click":
        pyautogui.click(x=step["x"], y=step["y"], button="right")
        return {"x": step["x"], "y": step["y"]}
    if a == "move":
        pyautogui.moveTo(step["x"], step["y"])
        return {"x": step["x"], "y": step["y"]}
    if a == "drag":
        pyautogui.moveTo(step["start_x"], step["start_y"])
        pyautogui.dragTo(step["end_x"], step["end_y"], button=step.get("button", "left"),
                         duration=step.get("duration", 0.2))
        return {}
    if a == "type":
        return _type(step["text"], step.get("interval", 0.0))
    if a == "key":
        return _press(step.get("key"), step.get("keys"))
    if a == "scroll":
        if step.get("x") is not None and step.get("y") is not None:
            pyautogui.scroll(step["delta"], x=step["x"], y=step["y"])
        else:
            pyautogui.scroll(step["delta"])
        return {"delta": step["delta"]}
    if a == "wait":
        ms = max(0, min(int(step.get("ms", 300)), 10000))
        time.sleep(ms / 1000)
        return {"ms": ms}
    if a == "active_window":
        return {"title": _active_title()}
    if a == "ui_click":
        return on_uia(uia_click, step.get("id"), step.get("name"), step.get("role"),
                      step.get("window_title"), step.get("method", "auto"),
                      step.get("button", "left"), step.get("clicks", 1))
    if a == "ui_set_text":
        return on_uia(uia_set_text, step["text"], step.get("id"), step.get("name"), step.get("role"),
                      step.get("window_title"), step.get("append", False))
    if a == "ui_get_text":
        return on_uia(uia_get_text, step.get("id"), step.get("name"), step.get("role"),
                      step.get("window_title"), step.get("max_chars", 200000))
    if a == "ui_select_text":
        return on_uia(uia_select_text, step["text"], step.get("id"), step.get("name"), step.get("role"),
                      step.get("window_title"))
    if a == "focus_window":
        return on_uia(_focus_window, step["window_title"])
    raise ValueError(f"unknown action {a!r}")


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

class ClickRequest(BaseModel):
    x: int
    y: int
    button: str = "left"
    clicks: int = 1


class MoveRequest(BaseModel):
    x: int
    y: int


class DragRequest(BaseModel):
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str = "left"
    duration: float = 0.2


class TypeRequest(BaseModel):
    text: str
    interval: float = 0.0


class KeyRequest(BaseModel):
    key: Optional[str] = None
    keys: Optional[List[str]] = None


class ScrollRequest(BaseModel):
    delta: int
    x: Optional[int] = None
    y: Optional[int] = None


class StepsRequest(BaseModel):
    steps: List[Dict[str, Any]]
    screenshot_after: bool = False
    region: Optional[Any] = None
    max_width: Optional[int] = 1600
    quality: int = 70
    settle_ms: int = 250


class UiTreeRequest(BaseModel):
    window_title: Optional[str] = None
    query: Optional[str] = None
    max_depth: int = 12
    limit: int = 150
    include_unnamed: bool = False


class UiClickRequest(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    window_title: Optional[str] = None
    method: str = "auto"
    button: str = "left"
    clicks: int = 1


class UiSetTextRequest(BaseModel):
    text: str
    id: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    window_title: Optional[str] = None
    append: bool = False


class BrowserTextRequest(BaseModel):
    tab_match: str = ""
    max_chars: int = 20000


@app.get("/health")
def health(x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    with _MSS() as sct:
        monitors = sct.monitors
    hwnd = win32gui.GetForegroundWindow()
    return {
        "status": "ok",
        "version": app.version,
        "monitors": monitors,
        "primary_size": list(pyautogui.size()),
        "active_window": {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd) if hwnd else "",
                          "rect": win32gui.GetWindowRect(hwnd) if hwnd else None},
        "cdp_available": _cdp_targets() is not None,
    }


@app.get("/screenshot")
def screenshot(max_width: Optional[int] = Query(1600), format: str = Query("jpeg"),
               quality: int = Query(70), region: Optional[str] = Query(None),
               max_bytes: Optional[int] = Query(DEFAULT_MAX_BYTES), x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    try:
        data, meta = capture(region, max_width or None, format, quality, max_bytes or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(content=data, media_type=f"image/{meta['format']}",
                    headers={"X-Screenshot-Meta": json.dumps(meta)})


@app.post("/click")
def click(req: ClickRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    pyautogui.click(x=req.x, y=req.y, button=req.button, clicks=req.clicks)
    return {"status": "ok", "action": "click", "x": req.x, "y": req.y, "button": req.button, "clicks": req.clicks}


@app.post("/move")
def move(req: MoveRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    pyautogui.moveTo(req.x, req.y)
    return {"status": "ok", "action": "move", "x": req.x, "y": req.y}


@app.post("/drag")
def drag(req: DragRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    do_action({"action": "drag", **req.model_dump()})
    return {"status": "ok", "action": "drag", "from": [req.start_x, req.start_y], "to": [req.end_x, req.end_y]}


@app.post("/type")
def type_text(req: TypeRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    _type(req.text, req.interval)
    return {"status": "ok", "action": "type", "chars": len(req.text)}


@app.post("/key")
def press_key(req: KeyRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    try:
        return {"status": "ok", "action": "key", **_press(req.key, req.keys)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/scroll")
def scroll(req: ScrollRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    do_action({"action": "scroll", **req.model_dump()})
    return {"status": "ok", "action": "scroll", "delta": req.delta}


@app.post("/steps")
def steps(req: StepsRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    if len(req.steps) > 50:
        raise HTTPException(status_code=400, detail="max 50 steps per batch")
    t0 = time.perf_counter()
    results = []
    ok = True
    for i, step in enumerate(req.steps):
        s0 = time.perf_counter()
        try:
            detail = do_action(step)
            if isinstance(detail, dict) and detail.get("status") == "error":
                raise RuntimeError(detail.get("error"))
            results.append({"i": i, "action": step.get("action"), "status": "ok", "detail": detail,
                            "ms": round((time.perf_counter() - s0) * 1000, 1)})
        except Exception as e:
            ok = False
            results.append({"i": i, "action": step.get("action"), "status": "error",
                            "error": f"{type(e).__name__}: {e}"})
            break
    out: Dict[str, Any] = {"status": "ok" if ok else "error",
                           "completed": sum(r["status"] == "ok" for r in results),
                           "total": len(req.steps), "results": results,
                           "active_window": _active_title()}
    if req.screenshot_after:
        time.sleep(max(0, min(req.settle_ms, 3000)) / 1000)
        try:
            data, meta = capture(req.region, req.max_width, "jpeg", req.quality)
            out["screenshot"] = {"meta": meta, "data_b64": base64.b64encode(data).decode()}
        except Exception as e:
            out["screenshot_error"] = str(e)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _uia_call(fn, *args, **kwargs):
    try:
        try:
            return on_uia(fn, *args, **kwargs)
        except (LookupError, ValueError, concurrent.futures.TimeoutError):
            raise
        except Exception:
            time.sleep(0.2)
            return on_uia(fn, *args, **kwargs)
    except (LookupError, ValueError) as e:
        return {"status": "error", "error": str(e)}
    except concurrent.futures.TimeoutError:
        return {"status": "error", "error": "UIA call timed out"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/ui/tree")
def ui_tree(req: UiTreeRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    return _uia_call(uia_tree, req.window_title, req.query, max(1, min(req.max_depth, 40)),
                     max(1, min(req.limit, 500)), req.include_unnamed)


@app.post("/ui/click")
def ui_click(req: UiClickRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    return _uia_call(uia_click, req.id, req.name, req.role, req.window_title, req.method, req.button, req.clicks)


@app.post("/ui/set_text")
def ui_set_text(req: UiSetTextRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    return _uia_call(uia_set_text, req.text, req.id, req.name, req.role, req.window_title, req.append)


@app.post("/browser/text")
def browser_text_ep(req: BrowserTextRequest, x_token: Optional[str] = Header(None)):
    check_auth(x_token)
    try:
        return browser_text(req.tab_match, max(500, min(req.max_chars, 200000)))
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    if not AUTH_TOKEN:
        sys.stderr.write(f"gui_bridge: refusing to start: GUI_BRIDGE_TOKEN not set (env or {ENV_FILE})\n")
        sys.exit(1)
    # Same bind as v1 (no new port). CDP stays on Chrome's own 127.0.0.1:9222.
    uvicorn.run(app, host="0.0.0.0", port=8780, log_level="warning")
