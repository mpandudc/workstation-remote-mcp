import functools
import re
import shlex
import sys
import argparse
import asyncio
import json
import logging
import base64
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from .config import config
from .executor import send_wol_magic_packet, run_ssh

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("workstation-remote-mcp")

mcp = FastMCP(
    name="workstation-remote-mcp",
)


# Administrative tools are absent from ordinary discovery and guarded on direct calls.
_ADMIN_ENABLED = False
_ADMIN_TOOLS = []

def admin_tool(fn):
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        if not _ADMIN_ENABLED:
            raise PermissionError("Administrative tool requires a separate explicit --admin stdio session")
        return await fn(*args, **kwargs)
    _ADMIN_TOOLS.append(guarded)
    return guarded

def enable_admin():
    global _ADMIN_ENABLED
    if not _ADMIN_ENABLED:
        for fn in _ADMIN_TOOLS:
            mcp.tool()(fn)
        _ADMIN_ENABLED = True


@mcp.tool()
def pc_wake(mac_address: Optional[str] = None) -> Dict[str, Any]:
    """Send a Wake-on-LAN (WOL) magic packet across LAN broadcast to power on the Windows PC.

    Args:
        mac_address: Optional custom MAC address. Defaults to configured MPDC-PC MAC (9C:6B:00:A2:31:54).
    """
    target_mac = mac_address or config.mac_address
    try:
        send_wol_magic_packet(target_mac)
        return {
            "status": "success",
            "message": f"Wake-on-LAN magic packet broadcasted to MAC `{target_mac}`.",
            "lan_broadcast_ports": [7, 9],
            "tip": "Wait ~15-30 seconds before checking `pc_status` while the motherboard POSTs and Windows boots."
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def pc_status() -> Dict[str, Any]:
    """Check online connectivity and basic health (RAM, OS uptime, CPU) of the Windows PC."""
    ps_cmd = 'powershell -NoProfile -NonInteractive -EncodedCommand JABQAHIAbwBnAHIAZQBzAHMAUAByAGUAZgBlAHIAZQBuAGMAZQA9ACcAUwBpAGwAZQBuAHQAbAB5AEMAbwBuAHQAaQBuAHUAZQAnADsAIAAkAG8APQBHAGUAdAAtAEMAaQBtAEkAbgBzAHQAYQBuAGMAZQAgAFcAaQBuADMAMgBfAE8AcABlAHIAYQB0AGkAbgBnAFMAeQBzAHQAZQBtADsAIAAkAGMAPQAoAEcAZQB0AC0AQwBpAG0ASQBuAHMAdABhAG4AYwBlACAAVwBpAG4AMwAyAF8AUAByAG8AYwBlAHMAcwBvAHIAIAB8ACAATQBlAGEAcwB1AHIAZQAtAE8AYgBqAGUAYwB0ACAALQBQAHIAbwBwAGUAcgB0AHkAIABMAG8AYQBkAFAAZQByAGMAZQBuAHQAYQBnAGUAIAAtAEEAdgBlAHIAYQBnAGUAKQAuAEEAdgBlAHIAYQBnAGUAOwAgAFsAcABzAGMAdQBzAHQAbwBtAG8AYgBqAGUAYwB0AF0AQAB7AEMAYQBwAHQAaQBvAG4APQAkAG8ALgBDAGEAcAB0AGkAbwBuADsAIABVAHAAdABpAG0AZQBIAG8AdQByAHMAPQBbAG0AYQB0AGgAXQA6ADoAUgBvAHUAbgBkACgAKAAoAEcAZQB0AC0ARABhAHQAZQApAC0AJABvAC4ATABhAHMAdABCAG8AbwB0AFUAcABUAGkAbQBlACkALgBUAG8AdABhAGwASABvAHUAcgBzACwAMQApADsAIABDAHAAdQBMAG8AYQBkAFAAZQByAGMAZQBuAHQAPQAkAGMAOwAgAFQAbwB0AGEAbABSAGEAbQBHAEIAPQBbAG0AYQB0AGgAXQA6ADoAUgBvAHUAbgBkACgAJABvAC4AVABvAHQAYQBsAFYAaQBzAGkAYgBsAGUATQBlAG0AbwByAHkAUwBpAHoAZQAvADEATQBCACwAMgApADsAIABGAHIAZQBlAFIAYQBtAEcAQgA9AFsAbQBhAHQAaABdADoAOgBSAG8AdQBuAGQAKAAkAG8ALgBGAHIAZQBlAFAAaAB5AHMAaQBjAGEAbABNAGUAbQBvAHIAeQAvADEATQBCACwAMgApAH0AIAB8ACAAQwBvAG4AdgBlAHIAdABUAG8ALQBKAHMAbwBuACAALQBDAG8AbQBwAHIAZQBzAHMA'

    # Health query (PowerShell + CIM) can be slow on a freshly booted or
    # memory-starved PC, so give it room and never equate a failed health
    # query with "offline" without a separate reachability probe.
    code, stdout, stderr = await run_ssh(ps_cmd, timeout=15)
    if code != 0:
        probe_code, probe_out, probe_err = await run_ssh("cmd /c echo ok", timeout=5)
        if probe_code == 0 and "ok" in probe_out:
            return {
                "online": True,
                "host": config.host_tailscale,
                "health": "unavailable",
                "error": stderr or f"Health query failed with exit code {code}",
            }
        return {
            "online": False,
            "host": config.host_tailscale,
            "reason": "timeout" if probe_code == 124 else "ssh_error",
            "error": probe_err or stderr or "Host unreachable via SSH. PC might be asleep or powered off.",
            "suggestion": "Call `pc_wake` to send a Wake-on-LAN packet.",
        }

    try:
        data = json.loads(stdout)
        total_ram = data.get("TotalRamGB", 0)
        free_ram = data.get("FreeRamGB", 0)
        used_ram = round(total_ram - free_ram, 2)
        ram_pct = round((used_ram / total_ram) * 100, 1) if total_ram else 0

        return {
            "online": True,
            "host": config.host_tailscale,
            "os": data.get("Caption"),
            "uptime_hours": data.get("UptimeHours"),
            "cpu_load_percent": data.get("CpuLoadPercent"),
            "ram": {
                "total_gb": total_ram,
                "used_gb": used_ram,
                "free_gb": free_ram,
                "usage_percent": ram_pct,
            }
        }
    except Exception as e:
        return {
            "online": True,
            "raw_output": stdout,
            "parse_error": str(e)
        }

@admin_tool
async def pc_lock() -> Dict[str, Any]:
    """Lock the active Windows session immediately (equivalent to Win + L)."""
    code, stdout, stderr = await run_ssh('rundll32.exe user32.dll,LockWorkStation', timeout=5)
    if code != 0:
        return {"status": "error", "error": stderr or "Failed to lock workstation."}
    return {"status": "success", "message": "Workstation locked successfully."}

@admin_tool
async def pc_sleep() -> Dict[str, Any]:
    """Put the Windows PC into S3 sleep / suspended mode via SSH. Can be woken up later via `pc_wake`."""
    # Using powershell SetSuspendState (Suspend, Force, DisableWakeEvent=False)
    ps_cmd = 'powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)"'
    code, stdout, stderr = await run_ssh(ps_cmd, timeout=5)
    return {
        "status": "triggered",
        "message": "Windows PC sleep command dispatched. Machine will enter S3 standby.",
        "wake_instruction": "Call `pc_wake` anytime to resume the machine."
    }

@mcp.tool()
async def pc_list_heavy_processes(top_n: int = 10) -> Dict[str, Any]:
    """List processes consuming the highest RAM or CPU on the Windows PC."""
    top_n = max(1, min(top_n, 30))
    ps_cmd = f'workstation-processes-v1 {top_n}'
    code, stdout, stderr = await run_ssh(ps_cmd, timeout=6)
    if code != 0:
        return {"status": "error", "error": stderr or "Failed fetching processes."}
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            data = [data]
        return {
            "top_processes_by_ram": data,
            "count": len(data)
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed parsing process list: {e}", "raw": stdout}


async def _ensure_gui_bridge() -> bool:
    """Checks if GUI bridge is responding. If not, triggers scheduled task to start it."""
    try:
        await asyncio.to_thread(_http, f"http://{config.host_tailscale}:{config.gui_bridge_port}/health", None, 2.5)
        return True
    except Exception:
        logger.info("GUI bridge unreachable, triggering on-demand start via SSH...")
        await run_ssh('schtasks /Run /TN "OpenBot-GUI-Bridge"', timeout=4)
        await asyncio.sleep(1.5)
        return False


_NO_TOKEN = ("GUI_BRIDGE_TOKEN is not set: put it in the environment or in "
             "WORKSTATION_ENV_FILE (default ~mpandudc/.config/workstation-remote-mcp/bridge.env)")


def _http(url: str, body: Optional[bytes], timeout: float):
    headers = {"x-token": config.gui_bridge_token}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


async def _gui_req(path: str, data: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    if not config.gui_bridge_token:
        return {"status": "error", "error": _NO_TOKEN}
    url = f"http://{config.host_tailscale}:{config.gui_bridge_port}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    for attempt in range(2):
        try:
            # Blocking urllib runs off the event loop so one slow bridge call never stalls the SSE server.
            raw, _ = await asyncio.to_thread(_http, url, body, timeout)
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            if attempt == 0:
                await _ensure_gui_bridge()
            else:
                return {"status": "error", "error": f"GUI bridge error on {path}: {e}. Ensure PC is awake and user logged in."}


async def _gui_fetch_image(params: Dict[str, Any], timeout: float = 15.0):
    """GET /screenshot -> (image bytes, meta dict from X-Screenshot-Meta)."""
    if not config.gui_bridge_token:
        raise RuntimeError(_NO_TOKEN)
    url = f"http://{config.host_tailscale}:{config.gui_bridge_port}/screenshot?{urllib.parse.urlencode(params)}"
    for attempt in range(2):
        try:
            raw, headers = await asyncio.to_thread(_http, url, None, timeout)
            meta_raw = next((v for k, v in headers.items() if k.lower() == "x-screenshot-meta"), None)
            meta = json.loads(meta_raw) if meta_raw else {"format": params.get("format", "jpeg"), "bytes": len(raw)}
            return raw, meta
        except Exception as e:
            if attempt == 0:
                await _ensure_gui_bridge()
            else:
                raise RuntimeError(f"Failed to capture Windows screenshot: {e}. Check `pc_status`.")


def _region_csv(region: Optional[List[int]]) -> Optional[str]:
    if region is None:
        return None
    if len(region) != 4:
        raise ValueError("region must be [x0, y0, x1, y1] in screen pixels")
    x0, y0, x1, y1 = (int(v) for v in region)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("region needs x1 > x0 and y1 > y0")
    return f"{x0},{y0},{x1},{y1}"


def _image_result(data: bytes, meta: Dict[str, Any], summary: Dict[str, Any]) -> ToolResult:
    fmt = meta.get("format", "jpeg")
    img = Image(data=data, format=fmt).to_image_content()
    return ToolResult(content=[img, TextContent(type="text", text=json.dumps(summary))])


_COORD_HINT = "screen_x = origin[0] + image_x * scale; screen_y = origin[1] + image_y * scale"


@mcp.tool()
async def pc_gui_status() -> Dict[str, Any]:
    """Check status of Windows GUI automation bridge, active window, screen resolution and CDP availability."""
    return await _gui_req("/health")


@mcp.tool()
async def pc_screenshot(
    region: Optional[List[int]] = None,
    max_width: Optional[int] = 1600,
    quality: int = 70,
    format: str = "jpeg",
) -> ToolResult:
    """Capture the Windows PC screen (primary monitor 3440x1440) as a compact JPEG.

    Prefer `pc_ui_tree` / `pc_browser_text` when you need names or text; use a
    `region` crop to look closely at one area. The text block returns `origin`
    and `scale` to map image pixels back to click coordinates.

    Args:
        region: Optional [x0, y0, x1, y1] crop in screen pixels.
        max_width: Max output width in pixels (default 1600; None = native).
        quality: JPEG quality 20-95 (default 70; auto-lowered to stay under ~150 KB).
        format: 'jpeg' (default) or 'png' (large; avoid unless needed).
    """
    params: Dict[str, Any] = {"format": "png" if format.lower() == "png" else "jpeg",
                              "quality": max(20, min(int(quality), 95))}
    params["max_width"] = int(max_width) if max_width else 0
    csv = _region_csv(region)
    if csv:
        params["region"] = csv
    data, meta = await _gui_fetch_image(params)
    return _image_result(data, meta, {**meta, "coords": _COORD_HINT})


@mcp.tool()
async def pc_run_steps(
    steps: List[Dict[str, Any]],
    screenshot_after: bool = False,
    region: Optional[List[int]] = None,
    max_width: int = 1600,
    settle_ms: int = 250,
) -> ToolResult:
    """Run several GUI actions in one call; stops at the first failing step.

    Each step is {"action": ..., ...}. Actions:
      click {x,y,button?,clicks?} | double_click {x,y} | right_click {x,y} | move {x,y}
      drag {start_x,start_y,end_x,end_y} | type {text} | key {key:"ctrl+l"} or {keys:[..]}
      scroll {delta,x?,y?} | wait {ms<=10000} | active_window {}
      focus_window {window_title} | ui_click {id|name, role?, window_title?, method?}
      ui_set_text {id|name, text, role?, window_title?, append?}
    Any step may add "expect_window": "<title substring>" to fail instead of acting
    when another window is in front (the PC is shared with its owner).

    Args:
        steps: 1-50 action objects, run in order.
        screenshot_after: Return one JPEG after the last step (after settle_ms).
        region: Optional [x0, y0, x1, y1] crop for that screenshot.
        max_width: Screenshot max width (default 1600).
        settle_ms: Wait before the final screenshot (default 250).
    """
    if not steps:
        raise ValueError("steps must contain at least one action")
    if len(steps) > 50:
        raise ValueError("max 50 steps per batch")
    body: Dict[str, Any] = {"steps": steps, "screenshot_after": screenshot_after,
                            "max_width": max_width, "settle_ms": settle_ms}
    csv = _region_csv(region)
    if csv:
        body["region"] = csv
    res = await _gui_req("/steps", body, timeout=120.0)
    shot = res.pop("screenshot", None)
    if shot:
        res["screenshot_meta"] = {**shot["meta"], "coords": _COORD_HINT}
        return _image_result(base64.b64decode(shot["data_b64"]), shot["meta"], res)
    return ToolResult(content=[TextContent(type="text", text=json.dumps(res))])


@mcp.tool()
async def pc_ui_tree(
    window_title: Optional[str] = None,
    query: Optional[str] = None,
    max_depth: int = 12,
    limit: int = 150,
    include_unnamed: bool = False,
) -> Dict[str, Any]:
    """List named UI elements (Windows UI Automation) of a window: id, role, name, rect.

    Use the returned `id` with `pc_ui_click` / `pc_ui_set_text` instead of guessing
    coordinates. Ids stay valid while the element exists; re-run after big UI changes.

    Args:
        window_title: Substring of a top-level window title; default = foreground window.
        query: Filter by name/automation-id substring, or an exact role (e.g. 'button', 'edit').
        max_depth: Tree depth to walk (default 12; browsers often need 20-30).
        limit: Max elements returned (default 150, max 500).
        include_unnamed: Also return elements without a name.
    """
    return await _gui_req("/ui/tree", {"window_title": window_title, "query": query, "max_depth": max_depth,
                                       "limit": limit, "include_unnamed": include_unnamed}, timeout=30.0)


@mcp.tool()
async def pc_ui_click(
    id: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    window_title: Optional[str] = None,
    method: str = "auto",
) -> Dict[str, Any]:
    """Click a UI element by id (from pc_ui_tree) or by visible name.

    method 'auto' uses the element's Invoke/Toggle/Select pattern (no mouse move)
    and falls back to a mouse click at its centre; 'mouse' always clicks.

    Args:
        id: Element id from pc_ui_tree.
        name: Element name (exact match preferred, else substring) when no id.
        role: Optional role filter for name lookup, e.g. 'Button', 'MenuItem'.
        window_title: Window to search for name lookup; default = foreground window.
        method: 'auto' (default), 'invoke' or 'mouse'.
    """
    if not id and not name:
        raise ValueError("give id or name")
    return await _gui_req("/ui/click", {"id": id, "name": name, "role": role,
                                        "window_title": window_title, "method": method}, timeout=30.0)


@mcp.tool()
async def pc_ui_set_text(
    text: str,
    id: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    window_title: Optional[str] = None,
    append: bool = False,
) -> Dict[str, Any]:
    """Set the text of an input element (UIA ValuePattern, else focus + select-all + type).

    Args:
        text: Text to put in the field.
        id: Element id from pc_ui_tree.
        name: Element name when no id.
        role: Optional role filter for name lookup (e.g. 'Edit').
        window_title: Window to search for name lookup; default = foreground window.
        append: Type after existing text instead of replacing it.
    """
    if not id and not name:
        raise ValueError("give id or name")
    return await _gui_req("/ui/set_text", {"text": text, "id": id, "name": name, "role": role,
                                           "window_title": window_title, "append": append}, timeout=30.0)


@mcp.tool()
async def pc_browser_text(tab_match: str = "", max_chars: int = 20000) -> Dict[str, Any]:
    """Read the active tab of a Chrome/Edge window on the Windows PC as text (UI Automation).

    Default (no tab_match) = topmost real browser window (Electron apps such as OpenBot are
    skipped). Only the active tab of each window is readable; canvas terminals (xterm.js)
    are not. For Proxmox work use `ssh pve` instead of the web shell.

    Args:
        tab_match: Substring of the window's active tab title (e.g. 'Proxmox').
        max_chars: Max characters of text returned (default 20000).
    """
    return await _gui_req("/browser/text", {"tab_match": tab_match, "max_chars": max_chars}, timeout=30.0)


@mcp.tool()
async def pc_click(x: int, y: int, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
    """Click mouse at (x, y) coordinates on the Windows PC display.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.
        button: 'left', 'right', or 'middle' (default 'left').
        clicks: 1 for single click, 2 for double click (default 1).
    """
    return await _gui_req("/click", {"x": x, "y": y, "button": button, "clicks": clicks})


@mcp.tool()
async def pc_mouse_move(x: int, y: int) -> Dict[str, Any]:
    """Move mouse cursor to (x, y) coordinates on the Windows PC display."""
    return await _gui_req("/move", {"x": x, "y": y})


@mcp.tool()
async def pc_mouse_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left") -> Dict[str, Any]:
    """Drag mouse from (start_x, start_y) to (end_x, end_y)."""
    return await _gui_req("/drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y, "button": button})


@mcp.tool()
async def pc_type(text: str) -> Dict[str, Any]:
    """Type text into the currently active window on the Windows PC."""
    return await _gui_req("/type", {"text": text})


@mcp.tool()
async def pc_key(key: str) -> Dict[str, Any]:
    """Press a single key or key combination on the Windows PC.

    Examples: 'enter', 'tab', 'esc', 'backspace', 'ctrl+c', 'ctrl+v', 'alt+tab', 'win+r'.
    """
    return await _gui_req("/key", {"key": key})


@mcp.tool()
async def pc_scroll(delta: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
    """Scroll mouse wheel. Positive delta scrolls up, negative scrolls down."""
    return await _gui_req("/scroll", {"delta": delta, "x": x, "y": y})


@admin_tool
async def pc_kill_process(name_or_pid: str) -> Dict[str, Any]:
    """Terminate a runaway or hung process on the Windows PC by name or PID.

    Args:
        name_or_pid: Process name (e.g. 'chrome', 'node') or integer PID.
    """
    # Keep both PowerShell and the Windows SSH shell boundary free of syntax.
    if re.fullmatch(r"[0-9]+", name_or_pid):
        if len(name_or_pid) > 10 or not 1 <= int(name_or_pid) <= 2147483647:
            raise ValueError("PID must be a positive signed 32-bit integer")
        ps_cmd = f'powershell -NoProfile -Command "Stop-Process -Id {int(name_or_pid)} -Force"'
    else:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,127}", name_or_pid):
            raise ValueError("Process name contains unsupported characters")
        clean_name = re.sub(r"\.exe$", "", name_or_pid, flags=re.IGNORECASE).strip()
        if not clean_name:
            raise ValueError("Process name cannot be empty")
        ps_cmd = f'powershell -NoProfile -Command "Stop-Process -Name \'{clean_name}\' -Force"'

    code, stdout, stderr = await run_ssh(ps_cmd, timeout=6)
    if code != 0:
        return {"status": "error", "error": stderr or f"Process {name_or_pid} not found or access denied."}
    return {"status": "success", "message": f"Killed process: {name_or_pid}."}

@admin_tool
async def pc_shutdown(delay_seconds: int = 0, force: bool = True) -> Dict[str, Any]:
    """Power off / shutdown the Windows PC completely (Power OFF).
    
    Args:
        delay_seconds: Delay before shutting down (default: 0 for instant).
        force: Force close running applications without prompting (default: True).
    """
    f_flag = "/f" if force else ""
    cmd = f"shutdown.exe /s {f_flag} /t {delay_seconds}"
    code, stdout, stderr = await run_ssh(cmd, timeout=5)
    if code != 0:
        return {"status": "error", "error": stderr or "Failed to initiate shutdown."}
    return {
        "status": "triggered",
        "action": "shutdown",
        "delay_seconds": delay_seconds,
        "message": "Windows PC shutdown initiated. To power back ON later, use `pc_wake`."
    }

@admin_tool
async def pc_reboot(delay_seconds: int = 0, force: bool = True) -> Dict[str, Any]:
    """Restart / reboot the Windows PC.
    
    Args:
        delay_seconds: Delay before restarting (default: 0 for instant).
        force: Force close running applications without prompting (default: True).
    """
    f_flag = "/f" if force else ""
    cmd = f"shutdown.exe /r {f_flag} /t {delay_seconds}"
    code, stdout, stderr = await run_ssh(cmd, timeout=5)
    if code != 0:
        return {"status": "error", "error": stderr or "Failed to initiate restart."}
    return {
        "status": "triggered",
        "action": "reboot",
        "delay_seconds": delay_seconds,
        "message": "Windows PC reboot initiated."
    }

def main():
    parser = argparse.ArgumentParser(description="Workstation Remote FastMCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--admin", action="store_true", help="Enable admin tools in a separate stdio session only")
    args = parser.parse_args()
    if not config.gui_bridge_token:
        sys.exit(f"workstation-remote-mcp: refusing to start: {_NO_TOKEN}")
    if args.admin:
        if args.transport != "stdio":
            parser.error("--admin is restricted to explicit stdio sessions; shared HTTP/SSE stays read-only")
        enable_admin()

    if args.transport in {"sse", "http"}:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
