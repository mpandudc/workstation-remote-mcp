import asyncio, unittest, importlib, os
from unittest.mock import patch, AsyncMock
from fastmcp import Client
PACKAGE=os.environ["MCP_TEST_PACKAGE"]
s=importlib.import_module(PACKAGE+".server")
PROX=PACKAGE.startswith("proxmox")
SAFE={"proxmox_cluster_status","lxc_list","lxc_get_info","lxc_service_status","tailscale_mesh_status"} if PROX else {"pc_wake","pc_status","pc_list_heavy_processes"}
ADMIN={"lxc_control_power","lxc_snapshot_create"} if PROX else {"pc_lock","pc_sleep","pc_kill_process","pc_shutdown","pc_reboot"}
ARGS={"lxc_control_power":{"vmid":203,"action":"reboot"},"lxc_snapshot_create":{"vmid":203,"snapname":"never"},"pc_kill_process":{"name_or_pid":"never"}}
class PolicyTests(unittest.IsolatedAsyncioTestCase):
 async def test_default_discovery_retains_safe_tools_only(self):
  async with Client(s.mcp) as c:self.assertEqual({t.name for t in await c.list_tools()},SAFE)
 async def test_default_admin_calls_never_reach_executor(self):
  with patch.object(s,"run_pvesh_json" if PROX else "run_ssh",new=AsyncMock(side_effect=AssertionError("NO EXECUTION"))) as executor:
   async with Client(s.mcp) as c:
    for name in ADMIN:
     with self.subTest(name=name):
      try:await c.call_tool(name,ARGS.get(name,{}))
      except Exception:pass
   executor.assert_not_called()
 async def test_default_direct_admin_functions_are_guarded(self):
  for name in ADMIN:
   with self.subTest(name=name):
    with self.assertRaises(PermissionError):await getattr(s,name)(**ARGS.get(name,{}))
 async def test_service_name_cannot_inject_shell(self):
  if not PROX:return
  with patch.object(s,"run_ssh_command",new=AsyncMock(return_value=(0,"ok",""))) as executor:
   async with Client(s.mcp) as c:
    for vmid,name in [(203,"docker;touch /tmp/never"),(203,"--help"),(0,"docker.service"),(203,"$(id)")]:
     with self.subTest(name=name,vmid=vmid):
      try:await c.call_tool("lxc_service_status",{"vmid":vmid,"service_name":name})
      except Exception:pass
   executor.assert_not_called()
 async def test_valid_service_name_read_path_retained(self):
  if not PROX:return
  with patch.object(s,"run_ssh_command",new=AsyncMock(return_value=(0,"active",""))) as executor:
   async with Client(s.mcp) as c:
    r=await c.call_tool("lxc_service_status",{"vmid":203,"service_name":"docker.service"})
    self.assertFalse(r.is_error)
   executor.assert_awaited_once()
if __name__=="__main__":unittest.main(verbosity=2)
