import importlib.util, pathlib, unittest

_p = pathlib.Path(__file__).resolve().parents[1] / "windows-bridge" / "gui_input.py"
_spec = importlib.util.spec_from_file_location("gui_input", _p)
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)


class NameMatch(unittest.TestCase):
    def test_empty_name_never_matches(self):
        cands = [("", "Custom"), ("", "Button")]
        self.assertIsNone(gi.pick_by_name(cands, "Don't save", "Button"))

    def test_curly_apostrophe_and_role(self):
        cands = [("", "Custom"), ("Save", "Button"), ("Don’t save", "Button"), ("Cancel", "Button")]
        self.assertEqual(gi.pick_by_name(cands, "Don't save", "Button"), 2)

    def test_role_filter_is_strict(self):
        cands = [("Don't save", "Text"), ("Don't save", "Button")]
        self.assertEqual(gi.pick_by_name(cands, "Don't save", "Button"), 1)
        self.assertIsNone(gi.pick_by_name([("Don't save", "Text")], "Don't save", "Button"))

    def test_exact_beats_partial(self):
        cands = [("Save as", "Button"), ("Save", "Button")]
        self.assertEqual(gi.pick_by_name(cands, "save"), 1)

    def test_accelerator_ignored_and_role_suffix(self):
        self.assertEqual(gi.pick_by_name([("&Save", "ButtonControl")], "Save", "button"), 0)

    def test_blank_query(self):
        self.assertIsNone(gi.pick_by_name([("x", "Button")], "  "))


class BrowserPick(unittest.TestCase):
    TITLES = ["OpenBot", "Untitled - Notepad", "homelab - Proxmox Virtual Environment - Google Chrome",
              "YouTube - Google Chrome"]

    def test_default_is_topmost_chrome_not_electron(self):
        self.assertEqual(gi.pick_browser_window(self.TITLES), 2)

    def test_match(self):
        self.assertEqual(gi.pick_browser_window(self.TITLES, "youtube"), 3)
        self.assertIsNone(gi.pick_browser_window(self.TITLES, "openbot"))


class Hotkeys(unittest.TestCase):
    def test_alt_f4(self):
        self.assertEqual(gi.parse_hotkey("alt+f4"), [0x12, 0x73])

    def test_keys_list_and_letters(self):
        self.assertEqual(gi.parse_hotkey(keys=["ctrl", "shift", "t"]), [0x11, 0x10, ord("T")])

    def test_single_and_plus(self):
        self.assertEqual(gi.parse_hotkey("enter"), [0x0D])
        self.assertEqual(gi.parse_hotkey("win+r"), [0x5B, ord("R")])

    def test_unknown(self):
        with self.assertRaises(ValueError):
            gi.parse_hotkey("hyper+q")


class CleanText(unittest.TestCase):
    def test_lone_surrogate_replaced(self):
        out = gi.clean_text("ok \ude00 \U0001F600")
        out.encode("utf-8")  # must not raise
        self.assertTrue(out.startswith("ok "))
        self.assertIn("\U0001F600", out)


class TypeInterval(unittest.TestCase):
    def test_floor_and_default(self):
        self.assertEqual(gi.type_interval(None), 0.02)
        self.assertEqual(gi.type_interval(0), 0.02)
        self.assertEqual(gi.type_interval(0.001), 0.015)
        self.assertEqual(gi.type_interval(0.05), 0.05)


class Utf16(unittest.TestCase):
    def test_capitals_and_surrogates(self):
        self.assertEqual(gi.utf16_units("Op"), [ord("O"), ord("p")])
        self.assertEqual(gi.utf16_units("\U0001F600"), [0xD83D, 0xDE00])


if __name__ == "__main__":
    unittest.main(verbosity=2)
