"""Alt+Tab and the other Alt shortcuts Windows keeps, sent to the remote."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import openvncviewer.ui as ui_module  # noqa: E402
from openvncviewer.history import ServerHistory  # noqa: E402
from openvncviewer.keyhook import (KeyboardHook, SystemKeyFilter,  # noqa: E402
                                   VK_ESCAPE, VK_F4, VK_SPACE, VK_TAB)
from openvncviewer.ui import ConnectDialog, MainWindow  # noqa: E402

VK_A = 0x41


class SystemKeyFilterTest(unittest.TestCase):
    def test_alt_tab_is_taken_while_the_remote_has_the_keyboard(self):
        keys = SystemKeyFilter()
        self.assertEqual(keys.take(VK_TAB, True, alt_down=True, active=True),
                         0xFF09)
        self.assertEqual(keys.take(VK_TAB, False, alt_down=True, active=True),
                         0xFF09)

    def test_each_system_shortcut_maps_to_its_keysym(self):
        for vk, keysym in ((VK_ESCAPE, 0xFF1B), (VK_SPACE, 0x20),
                           (VK_F4, 0xFFC1)):
            with self.subTest(vk=hex(vk)):
                self.assertEqual(SystemKeyFilter().take(vk, True, True, True),
                                 keysym)

    def test_without_alt_the_key_is_left_to_the_view(self):
        """Plain Tab already reaches the remote through Qt."""
        self.assertIsNone(SystemKeyFilter().take(VK_TAB, True, False, True))

    def test_nothing_is_taken_when_the_remote_lacks_focus(self):
        """Alt+Tab in another window, or the sidebar, is Windows' to handle."""
        self.assertIsNone(SystemKeyFilter().take(VK_TAB, True, True, False))

    def test_other_keys_are_never_taken(self):
        self.assertIsNone(SystemKeyFilter().take(VK_A, True, True, True))

    def test_a_taken_key_is_released_even_after_alt_or_focus_goes(self):
        """Otherwise the remote holds Tab down for good."""
        keys = SystemKeyFilter()
        keys.take(VK_TAB, True, alt_down=True, active=True)
        self.assertEqual(keys.take(VK_TAB, False, alt_down=False, active=False),
                         0xFF09)

    def test_a_release_for_a_press_not_taken_is_left_alone(self):
        self.assertIsNone(SystemKeyFilter().take(VK_TAB, False, True, True))


@unittest.skipUnless(sys.platform == "win32", "Windows keyboard hook")
class KeyboardHookTest(unittest.TestCase):
    def test_installs_and_uninstalls(self):
        hook = KeyboardHook(lambda: False, lambda keysym, down: None)
        self.addCleanup(hook.uninstall)
        self.assertTrue(hook.install())
        self.assertTrue(hook.installed)
        self.assertTrue(hook.install(), "a second install is harmless")
        hook.uninstall()
        self.assertFalse(hook.installed)
        hook.uninstall()  # and so is a second uninstall


class FakeHook:
    def __init__(self):
        self.installed = False

    def install(self):
        self.installed = True
        return True

    def uninstall(self):
        self.installed = False


class WindowWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = ServerHistory(Path(directory.name) / "servers.json")

        class DummyClient:
            def __init__(self, *args, **kwargs):
                self.keys = []
                self.desktop_name = "Stub desktop"
                self.width, self.height = 200, 120
                self.framebuffer = bytearray(200 * 120 * 4)

            def start(self):
                pass

            def stop(self):
                pass

            def send_clipboard(self, text):
                return True

            def send_key(self, keysym, down):
                self.keys.append((keysym, down))

        original = ui_module.RFBClient
        ui_module.RFBClient = DummyClient
        self.addCleanup(setattr, ui_module, "RFBClient", original)

        self.window = MainWindow()
        self.window.history = self.history
        self.window._key_hook = self.hook = FakeHook()
        self.addCleanup(self.window.close)

    def test_the_hook_is_in_place_only_while_connected(self):
        self.window.connect_to("mac.local", 5900, "amy", "pw")
        self.assertTrue(self.hook.installed)
        self.window.disconnect()
        self.assertFalse(self.hook.installed)

    def test_unticking_the_option_leaves_alt_tab_to_windows(self):
        self.window.connect_to("mac.local", 5900, "amy", "pw",
                               send_alt_shortcuts=False)
        self.assertFalse(self.hook.installed)

    def test_the_remote_has_the_keyboard_only_when_the_view_has_focus(self):
        self.window.connect_to("mac.local", 5900, "amy", "pw")
        self.window.show()
        self.window.activateWindow()
        self.window.view.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._remote_has_keyboard())

        self.window.host_panel.list_widget.setFocus()
        QApplication.processEvents()
        self.assertFalse(self.window._remote_has_keyboard())

    def test_taken_keys_go_to_the_session(self):
        self.window.connect_to("mac.local", 5900, "amy", "pw")
        self.window._send_system_key(0xFF09, True)
        self.assertEqual(self.window.client.keys, [(0xFF09, True)])

    def test_the_setting_is_remembered_per_server(self):
        self.window.connect_to("mac.local", 5900, "amy", "pw",
                               send_alt_shortcuts=False)
        self.window._on_resize(200, 120)
        self.assertFalse(self.history.find("mac.local")["send_alt_shortcuts"])

    def test_the_sidebar_connects_with_the_saved_setting(self):
        self.history.remember("mac.local", 5900, "amy",
                              send_alt_shortcuts=False)
        calls = []
        self.window.connect_to = lambda *args, **kwargs: calls.append(kwargs)
        self.window._on_panel_connect_requested("mac.local", 5900, "")
        self.assertFalse(calls[0]["send_alt_shortcuts"])


class DialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = ServerHistory(Path(directory.name) / "servers.json")

    def test_on_by_default_for_every_connection_type(self):
        for username in ("amy", ""):
            with self.subTest(username=username):
                dialog = ConnectDialog("new.host", 5900, username,
                                       history=self.history)
                self.addCleanup(dialog.close)
                self.assertTrue(dialog.send_alt_shortcuts.isChecked())

    def test_selecting_a_saved_server_restores_it(self):
        self.history.remember("mac.local", 5900, "amy",
                              send_alt_shortcuts=False)
        dialog = ConnectDialog(history=self.history)
        self.addCleanup(dialog.close)
        dialog.host.setEditText("mac.local")
        self.assertFalse(dialog.send_alt_shortcuts.isChecked())

    def test_a_server_saved_before_the_option_existed_has_it_on(self):
        self.history.remember("old.mac", 5900, "amy")
        self.assertTrue(self.history.find("old.mac")["send_alt_shortcuts"])


if __name__ == "__main__":
    unittest.main()
