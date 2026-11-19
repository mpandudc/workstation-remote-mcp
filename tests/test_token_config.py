import importlib, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MCP_TEST_PACKAGE", "workstation_remote_mcp")
cfg = importlib.import_module("workstation_remote_mcp.config")
srv = importlib.import_module("workstation_remote_mcp.server")


class LoadToken(unittest.TestCase):
    def test_env_var_wins(self):
        self.assertEqual(cfg.load_bridge_token({"GUI_BRIDGE_TOKEN": "abc"}, "/nonexistent"), "abc")

    def test_reads_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bridge.env"
            p.write_text("# comment\nOTHER=1\nGUI_BRIDGE_TOKEN = 'xyz123'\n")
            self.assertEqual(cfg.load_bridge_token({}, str(p)), "xyz123")

    def test_missing_gives_empty_never_default(self):
        self.assertEqual(cfg.load_bridge_token({}, "/nonexistent/bridge.env"), "")

    def test_no_hardcoded_default_in_source(self):
        import re
        for path in (Path(cfg.__file__), Path(__file__).resolve().parents[1] / "windows-bridge" / "gui_bridge.py"):
            src = path.read_text()
            # no os.environ.get("GUI_BRIDGE_TOKEN", "<literal>") style fallback anywhere
            self.assertIsNone(re.search(r"GUI_BRIDGE_TOKEN['\"]\s*,\s*['\"][^'\"]+['\"]", src), path.name)


class FailFast(unittest.TestCase):
    def test_main_exits_without_token(self):
        with patch.object(srv.config, "gui_bridge_token", ""), \
             patch.object(sys, "argv", ["x", "--transport", "sse"]), \
             patch.object(srv.mcp, "run", side_effect=AssertionError("must not start")):
            with self.assertRaises(SystemExit) as cm:
                srv.main()
        self.assertIn("GUI_BRIDGE_TOKEN", str(cm.exception.code))

    async def _noop(self):
        pass


class GuiReqWithoutToken(unittest.IsolatedAsyncioTestCase):
    async def test_gui_call_reports_missing_token(self):
        with patch.object(srv.config, "gui_bridge_token", ""):
            r = await srv._gui_req("/health")
        self.assertEqual(r["status"], "error")
        self.assertIn("GUI_BRIDGE_TOKEN", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
