import base64, json, os, importlib, unittest
from unittest.mock import AsyncMock, patch
from fastmcp import Client

os.environ.setdefault("MCP_TEST_PACKAGE", "workstation_remote_mcp")
s = importlib.import_module("workstation_remote_mcp.server")

JPEG = b"\xff\xd8\xff\xe0fakejpeg"
META = {"format": "jpeg", "bytes": len(JPEG), "width": 1600, "height": 670, "quality": 70,
        "origin": [0, 0], "source_size": [3440, 1440], "scale": 2.15}


def _texts(result):
    return [c.text for c in result.content if getattr(c, "type", "") == "text"]


def _images(result):
    return [c for c in result.content if getattr(c, "type", "") == "image"]


class ScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_jpeg_and_returns_meta(self):
        fetch = AsyncMock(return_value=(JPEG, META))
        with patch.object(s, "_gui_fetch_image", new=fetch):
            async with Client(s.mcp) as c:
                r = await c.call_tool("pc_screenshot", {})
        params = fetch.await_args.args[0]
        self.assertEqual(params["format"], "jpeg")
        self.assertEqual(params["quality"], 70)
        self.assertEqual(params["max_width"], 1600)
        self.assertNotIn("region", params)
        img = _images(r)
        self.assertEqual(len(img), 1)
        self.assertEqual(img[0].mimeType, "image/jpeg")
        meta = json.loads(_texts(r)[0])
        self.assertEqual(meta["scale"], 2.15)
        self.assertEqual(meta["origin"], [0, 0])

    async def test_region_list_is_sent_as_csv(self):
        fetch = AsyncMock(return_value=(JPEG, {**META, "origin": [100, 200], "scale": 1.0}))
        with patch.object(s, "_gui_fetch_image", new=fetch):
            async with Client(s.mcp) as c:
                await c.call_tool("pc_screenshot", {"region": [100, 200, 900, 600], "max_width": 800})
        params = fetch.await_args.args[0]
        self.assertEqual(params["region"], "100,200,900,600")
        self.assertEqual(params["max_width"], 800)

    async def test_bad_region_never_reaches_bridge(self):
        fetch = AsyncMock(return_value=(JPEG, META))
        with patch.object(s, "_gui_fetch_image", new=fetch):
            async with Client(s.mcp) as c:
                r = await c.call_tool("pc_screenshot", {"region": [5, 5, 1]}, raise_on_error=False)
        self.assertTrue(r.is_error)
        fetch.assert_not_awaited()


class StepsTests(unittest.IsolatedAsyncioTestCase):
    async def test_steps_posted_and_screenshot_split_out(self):
        bridge = {"status": "ok", "completed": 2, "total": 2, "results": [],
                  "screenshot": {"meta": META, "data_b64": base64.b64encode(JPEG).decode()}}
        req = AsyncMock(return_value=bridge)
        steps = [{"action": "click", "x": 1, "y": 2}, {"action": "key", "key": "enter"}]
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                r = await c.call_tool("pc_run_steps", {"steps": steps, "screenshot_after": True,
                                                       "region": [0, 0, 800, 400]})
        path, body = req.await_args.args[:2]
        self.assertEqual(path, "/steps")
        self.assertEqual(body["steps"], steps)
        self.assertTrue(body["screenshot_after"])
        self.assertEqual(body["region"], "0,0,800,400")
        self.assertEqual(len(_images(r)), 1)
        summary = json.loads(_texts(r)[0])
        self.assertNotIn("data_b64", json.dumps(summary))
        self.assertEqual(summary["screenshot_meta"]["scale"], 2.15)

    async def test_empty_steps_rejected(self):
        req = AsyncMock()
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                r = await c.call_tool("pc_run_steps", {"steps": []}, raise_on_error=False)
        self.assertTrue(r.is_error)
        req.assert_not_awaited()


class UiaTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_tree_forwards_filters(self):
        req = AsyncMock(return_value={"status": "ok", "elements": []})
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                await c.call_tool("pc_ui_tree", {"window_title": "Chrome", "query": "address", "max_depth": 9})
        path, body = req.await_args.args[:2]
        self.assertEqual(path, "/ui/tree")
        self.assertEqual(body["window_title"], "Chrome")
        self.assertEqual(body["query"], "address")
        self.assertEqual(body["max_depth"], 9)

    async def test_ui_click_needs_id_or_name(self):
        req = AsyncMock()
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                r = await c.call_tool("pc_ui_click", {}, raise_on_error=False)
        self.assertTrue(r.is_error)
        req.assert_not_awaited()

    async def test_ui_click_and_set_text_paths(self):
        req = AsyncMock(return_value={"status": "ok"})
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                await c.call_tool("pc_ui_click", {"name": "OK", "role": "Button"})
                self.assertEqual(req.await_args.args[0], "/ui/click")
                self.assertEqual(req.await_args.args[1]["name"], "OK")
                await c.call_tool("pc_ui_set_text", {"id": "e42.1", "text": "hello"})
                self.assertEqual(req.await_args.args[0], "/ui/set_text")
                self.assertEqual(req.await_args.args[1]["text"], "hello")

    async def test_browser_text(self):
        req = AsyncMock(return_value={"status": "ok", "backend": "uia", "text": "x"})
        with patch.object(s, "_gui_req", new=req):
            async with Client(s.mcp) as c:
                await c.call_tool("pc_browser_text", {"tab_match": "Proxmox"})
        path, body = req.await_args.args[:2]
        self.assertEqual(path, "/browser/text")
        self.assertEqual(body["tab_match"], "Proxmox")


if __name__ == "__main__":
    unittest.main(verbosity=2)
