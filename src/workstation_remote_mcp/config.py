import os
from pydantic import BaseModel, Field

DEFAULT_ENV_FILE = os.path.expanduser("~mpandudc/.config/workstation-remote-mcp/bridge.env")


def load_bridge_token(environ, env_file: str) -> str:
    """GUI_BRIDGE_TOKEN from the environment, else from a KEY=VALUE env file. Never a default."""
    tok = (environ.get("GUI_BRIDGE_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip() == "GUI_BRIDGE_TOKEN":
                    return val.strip().strip("'\"")
    except OSError:
        pass
    return ""


class WorkstationConfig(BaseModel):
    user: str = Field(
        default_factory=lambda: os.environ.get("WORKSTATION_USER", "mpand"),
        description="SSH user on workstation"
    )
    host_tailscale: str = Field(
        default_factory=lambda: os.environ.get("WORKSTATION_TS_IP", "100.116.2.118"),
        description="Tailscale IP of workstation"
    )
    host_lan: str = Field(
        default_factory=lambda: os.environ.get("WORKSTATION_LAN_IP", "192.168.0.102"),
        description="Local LAN IP of workstation"
    )
    gui_bridge_port: int = Field(
        default_factory=lambda: int(os.environ.get("WORKSTATION_GUI_PORT", "8780")),
        description="Port for Windows GUI automation bridge"
    )
    gui_bridge_token: str = Field(
        default_factory=lambda: load_bridge_token(os.environ, os.environ.get("WORKSTATION_ENV_FILE", DEFAULT_ENV_FILE)),
        description="Token for Windows GUI bridge auth (env GUI_BRIDGE_TOKEN or env file; no default)",
        repr=False,
    )
    mac_address: str = Field(
        default_factory=lambda: os.environ.get("WORKSTATION_MAC", "9C:6B:00:A2:31:54"),
        description="Workstation Ethernet MAC for Wake-on-LAN"
    )
    ssh_timeout: int = Field(default=5, description="SSH timeout in seconds")

config = WorkstationConfig()
