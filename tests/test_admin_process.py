import importlib, unittest
from unittest.mock import patch, AsyncMock
s=importlib.import_module("workstation_remote_mcp.server")
class ProcessBoundaryTests(unittest.IsolatedAsyncioTestCase):
 async def test_admin_rejects_shell_syntax_before_executor(self):
  with patch.object(s,"_ADMIN_ENABLED",True),patch.object(s,"run_ssh",new=AsyncMock(return_value=(0,"",""))) as executor:
   for value in ["node;whoami", "node$(whoami)", "node`whoami", "node'", 'node"', "node*", "node?", "--help", "node\nwhoami", "0", "4294967296"]:
    with self.subTest(value=value):
     with self.assertRaises(ValueError):await s.pc_kill_process(value)
   executor.assert_not_called()
 async def test_normal_name_with_spaces_is_literal(self):
  with patch.object(s,"_ADMIN_ENABLED",True),patch.object(s,"run_ssh",new=AsyncMock(return_value=(0,"",""))) as executor:
   await s.pc_kill_process("My Process.exe")
   self.assertIn("-Name 'My Process' -Force",executor.call_args.args[0])
 async def test_pid_uses_numeric_argument(self):
  with patch.object(s,"_ADMIN_ENABLED",True),patch.object(s,"run_ssh",new=AsyncMock(return_value=(0,"",""))) as executor:
   await s.pc_kill_process("1234")
   self.assertIn("-Id 1234 -Force",executor.call_args.args[0])
if __name__=="__main__":unittest.main(verbosity=2)
