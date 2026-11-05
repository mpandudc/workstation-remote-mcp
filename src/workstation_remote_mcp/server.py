import functools
import re
import shlex
import argparse
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from fastmcp import FastMCP
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

    code, stdout, stderr = await run_ssh(ps_cmd, timeout=10)
    if code != 0:
        return {
            "online": False,
            "host": config.host_tailscale,
            "error": stderr or "Host unreachable via SSH. PC might be asleep or powered off.",
            "suggestion": "Call `pc_wake` to send a Wake-on-LAN packet."
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
