# Workstation Remote FastMCP

A lightweight Model Context Protocol (MCP) server built with **FastMCP** to control, inspect, and automate a Windows PC / workstation remotely via Wake-on-LAN and SSH/PowerShell.

## Features
- **`pc_wake`**: Power **ON** the PC via Wake-on-LAN (WOL) magic packet across LAN broadcast (ports 7, 9).
- **`pc_shutdown`**: Power **OFF** / complete shutdown of Windows PC (`shutdown.exe /s /f /t 0`).
- **`pc_reboot`**: Restart / reboot Windows PC (`shutdown.exe /r /f /t 0`).
- **`pc_sleep`**: Suspend workstation to S3 standby mode.
- **`pc_status`**: Check online state, Windows OS caption, uptime, CPU load percentage, and RAM usage.
- **`pc_lock`**: Instantly lock workstation session (`rundll32.exe user32.dll,LockWorkStation`).
- **`pc_list_heavy_processes`**: Identify processes consuming top RAM/CPU.
- **`pc_kill_process`**: Terminate hung or runaway processes by name or PID.

## Configuration
Configurable via environment variables or default constants:
- `WORKSTATION_USER`: Default `mpand`
- `WORKSTATION_TS_IP`: Default `100.116.2.118` (Tailscale IP)
- `WORKSTATION_LAN_IP`: Default `192.168.0.102` (LAN IP)
- `WORKSTATION_MAC`: Default `9C:6B:00:A2:31:54` (Ethernet MAC)

## License
MIT

## Safety policy

Default sessions expose safe tools only. Proxmox power/snapshot and workstation lock/sleep/kill/shutdown/reboot are not registered and reject direct calls. Workstation wake remains available for recovery.

For a human-approved maintenance operation, launch a separate local stdio process with `--admin` and separately provisioned administrative credentials. Admin is rejected on HTTP/SSE. Never add admin processes to ordinary Hermes or shared units. This is an application guard, not OS credential isolation; keep administrative SSH credentials inaccessible to ordinary agent sessions.

Installed runtime: Python 3.11.16, FastMCP 4.0.5, Pydantic 2.13.5; `runtime-dependencies.txt` records all installed versions. Refresh intentionally with tests and rollback.
