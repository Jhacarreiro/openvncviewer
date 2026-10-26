"""Typing the session's own password into the Mac, from the sidebar.

The password a session logged in with is usually the macOS account password,
which is the one the Mac then asks for again - at the lock screen, in an admin
prompt. This types it as key presses, and the rules that matter are:

  * exactly the password goes out, once, and never Enter or Tab, which would
    act on the Mac rather than type into the field;
  * it is offered only while a macOS session is up, since a standard VNC
    password unlocks nothing on the Mac;
  * ending the session stops it, so the rest of a password never lands on
    whatever comes next.
"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from openvncviewer import ui as ui_module  # noqa: E402
from openvncviewer.history import ServerHistory  # noqa: E402
from openvncviewer.ui import MainWindow  # noqa: E402

HOST, PORT = "mac.local", 5900
REMOTE_W, REMOTE_H = 640, 480
RETURN, TAB = 0xFF0D, 0xFF09


def setUpModule():
    # MainWindow installs a real, system-wide keyboard hook whenever it
    # connects. Every keystroke on the machine running the suite would wait on
    # it, so these tests get a stand-in; test_keyhook covers the real one.
    patcher = mock.patch("openvncviewer.ui.KeyboardHook")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeClient:
    """An RFBClient that opens no socket and records the keys it is sent."""

    created = []

    def __init__(self, host, port, username, password, **callbacks):
        self.desktop_name = "Stub desktop"
        self.framebuffer = bytearray(REMOTE_W * REMOTE_H * 4)
        self.was_connected = False
        self.auth_failed = False
        self.keys = []
        self._lock = threading.Lock()
        FakeClient.created.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def send_clipboard(self, text):
        return True

    def send_key(self, keysym, down):
        with self._lock:
            self.keys.append((keysym, down))

    def pressed(self):
        with self._lock:
            return [keysym for keysym, down in self.keys if down]


class TypePasswordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        FakeClient.created = []
        original = ui_module.RFBClient
        ui_module.RFBClient = FakeClient
        self.addCleanup(setattr, ui_module, "RFBClient", original)

        self.warnings = []
        warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(
            lambda *args, **kwargs: self.warnings.append(args))
        self.addCleanup(setattr, QMessageBox, "warning", warning)

        self.window = MainWindow()
        self.window.history = ServerHistory(Path(directory.name) / "servers.json")
        self.addCleanup(self.window.close)
        self.addCleanup(self.window.view._cancel_text_input)
        self.button = self.window.host_panel.type_password_btn

    def establish(self, username="amy", password="secret"):
        self.window.connect_to(HOST, PORT, username, password)
        FakeClient.created[-1].was_connected = True
        self.window._on_resize(REMOTE_W, REMOTE_H)
        return FakeClient.created[-1]

    def wait_until(self, condition, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("timed out")
            self.app.processEvents()
            time.sleep(0.01)

    def test_types_exactly_the_password_and_never_enter(self):
        password = "pa$$ W0rd~é€"
        client = self.establish(password=password)

        self.button.click()
        self.button.click()  # a double click must not type it twice
        self.wait_until(lambda: self.window.view._text_sender is None)

        self.assertEqual(client.pressed(), [ui_module.TextSender._char_to_keysym(c)
                                            for c in password])
        self.assertNotIn(RETURN, client.pressed())
        downs = sum(1 for _, down in client.keys if down)
        self.assertEqual(downs * 2, len(client.keys), "a key was left held down")

    def test_offered_only_while_a_macos_session_is_up(self):
        self.assertFalse(self.button.isEnabled(), "offered with no session")

        self.window.connect_to(HOST, PORT, "amy", "secret")
        self.assertFalse(self.button.isEnabled(), "offered before the login")

        FakeClient.created[-1].was_connected = True
        self.window._on_resize(REMOTE_W, REMOTE_H)
        self.assertTrue(self.button.isEnabled())
        self.assertIn(HOST, self.button.toolTip(), "does not name the Mac")

        self.window.disconnect()
        self.assertFalse(self.button.isEnabled(), "still offered after Disconnect")

        # A standard VNC password unlocks the VNC server, not the Mac.
        self.establish(username="", password="vncpass")
        self.assertFalse(self.button.isEnabled())

    def test_a_password_with_tab_or_enter_is_refused_whole(self):
        for password in ("first\tsecond", "secret\n", "bell\x07"):
            with self.subTest(password=repr(password)):
                client = self.establish(password=password)
                self.warnings.clear()

                self.window.type_connection_password()
                time.sleep(0.2)

                self.assertEqual(client.keys, [], "sent part of the password")
                self.assertEqual(len(self.warnings), 1, "refused silently")
                self.assertNotIn(password, repr(self.warnings),
                                 "the warning shows the password")

    def test_ending_the_session_stops_the_typing(self):
        client = self.establish(password="x" * 200)

        self.window.type_connection_password()
        self.wait_until(lambda: client.pressed())
        self.window.disconnect()
        typed = len(client.pressed())
        time.sleep(0.3)

        # The key held at the moment of stopping is still released; nothing
        # new is pressed.
        self.assertEqual(len(client.pressed()), typed,
                         "kept typing after Disconnect")
        self.assertLess(typed, 200)
        self.assertIsNone(self.window.view._text_sender)


if __name__ == "__main__":
    unittest.main()
