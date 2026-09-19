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
    ps_cmd = (
        'powershell -NoProfile -Command "'
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
        "[PSCustomObject]@{ "
        "Caption = $os.Caption; "
        "UptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1); "
        "TotalRamGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2); "
        "FreeRamGB = [math]::Round($os.FreePhysicalMemory / 1MB, 2); "
        "CpuLoadPercent = $cpu "
        "} | ConvertTo-Json -Compress\""
    )

    code, stdout, stderr = await run_ssh(ps_cmd, timeout=4)
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

@mcp.tool()
async def pc_lock() -> Dict[str, Any]:
    """Lock the active Windows session immediately (equivalent to Win + L)."""
    code, stdout, stderr = await run_ssh('rundll32.exe user32.dll,LockWorkStation', timeout=5)
    if code != 0:
        return {"status": "error", "error": stderr or "Failed to lock workstation."}
    return {"status": "success", "message": "Workstation locked successfully."}

@mcp.tool()
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
    ps_cmd = (
        f'powershell -NoProfile -Command "'
        f"Get-Process | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First {top_n} "
        "ProcessName, Id, @{Name='WorkingSetMB';Expression={[math]::Round($_.WorkingSet64/1MB,1)}} | "
        'ConvertTo-Json -Compress"'
    )
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

@mcp.tool()
async def pc_kill_process(name_or_pid: str) -> Dict[str, Any]:
    """Terminate a runaway or hung process on the Windows PC by name or PID.

    Args:
        name_or_pid: Process name (e.g. 'chrome', 'node') or integer PID.
    """
    if name_or_pid.isdigit():
        ps_cmd = f'powershell -NoProfile -Command "Stop-Process -Id {name_or_pid} -Force"'
    else:
        clean_name = name_or_pid.replace(".exe", "").strip()
        ps_cmd = f'powershell -NoProfile -Command "Stop-Process -Name {clean_name} -Force"'

    code, stdout, stderr = await run_ssh(ps_cmd, timeout=6)
    if code != 0:
        return {"status": "error", "error": stderr or f"Process {name_or_pid} not found or access denied."}
    return {"status": "success", "message": f"Killed process: {name_or_pid}."}

def main():
    parser = argparse.ArgumentParser(description="Workstation Remote FastMCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    if args.transport in {"sse", "http"}:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
