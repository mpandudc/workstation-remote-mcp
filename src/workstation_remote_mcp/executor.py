import asyncio
import socket
from typing import Optional, Tuple
from .config import config

def send_wol_magic_packet(mac: Optional[str] = None) -> bool:
    """Send Wake-on-LAN magic packet over broadcast UDP."""
    target_mac = (mac or config.mac_address).replace(":", "").replace("-", "").strip().lower()
    if len(target_mac) != 12:
        raise ValueError(f"Invalid MAC address: {mac or config.mac_address}")

    data = bytes.fromhex("FF" * 6 + target_mac * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in [("192.168.0.255", 9), ("192.168.0.255", 7), ("255.255.255.255", 9)]:
            try:
                s.sendto(data, target)
            except Exception:
                pass
    return True

async def run_ssh(cmd: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """Execute command on Windows workstation via SSH."""
    timeout_val = timeout or config.ssh_timeout
    target_host = config.host_tailscale
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout_val}",
        f"{config.user}@{target_host}",
        cmd,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val + 2)
        return proc.returncode or 0, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return 124, "", f"SSH timeout after {timeout_val}s connecting to {target_host}"
    except Exception as e:
        return 1, "", str(e)
