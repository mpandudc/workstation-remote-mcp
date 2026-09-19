import os
from pydantic import BaseModel, Field

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
    mac_address: str = Field(
        default_factory=lambda: os.environ.get("WORKSTATION_MAC", "9C:6B:00:A2:31:54"),
        description="Workstation Ethernet MAC for Wake-on-LAN"
    )
    ssh_timeout: int = Field(default=5, description="SSH timeout in seconds")

config = WorkstationConfig()
