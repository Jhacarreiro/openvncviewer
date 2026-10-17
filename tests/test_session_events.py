"""Queued network callbacks must affect only the session that emitted them."""

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from openvncviewer import ui  # noqa: E402
from tests.test_auto_reconnect import FakeClient  # noqa: E402


class EventClient(FakeClient):
    def __init__(self, *args, **callbacks):
        super().__init__(*args, **callbacks)
        self.callbacks = callbacks


class SessionEventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            mock.patch.object(ui, "KeyboardHook"),
            mock.patch.object(ui, "RFBClient", EventClient),
            mock.patch.object(ui, "default_history_path",
                              return_value=Path(directory.name) / "servers.json"),
            mock.patch.object(ui.QMessageBox, "warning"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clipboard = mock.Mock()
        self.clipboard.text.return_value = ""
        patcher = mock.patch.object(QApplication, "clipboard", return_value=self.clipboard)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = ui.MainWindow()
        self.addCleanup(self.window.close)

    def connect(self, host):
        self.window.connect_to(host, 5900, "", "", share_clipboard=True)
        return self.window.client

    def queue(self, client, callback, *args):
        # Emitting off the GUI thread exercises Qt's real queued delivery.
        worker = threading.Thread(target=client.callbacks[callback], args=args)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_old_disconnect_does_not_tear_down_its_replacement(self):
        old = self.connect("old.example")
        self.queue(old, "on_disconnect", "old session ended")
        replacement = self.connect("new.example")
        self.app.processEvents()
        self.assertIs(self.window.client, replacement)
        self.assertFalse(replacement.stopped)
        self.assertIn("Connecting to new.example", self.window.status.text())

    def test_old_resize_cannot_commit_an_unconnected_replacements_history(self):
        old = self.connect("old.example")
        self.queue(old, "on_resize", old.width, old.height)
        replacement = self.connect("new.example")
        self.app.processEvents()
        self.assertIsNone(self.window.history.find("new.example"))
        self.assertIn("Connecting to new.example", self.window.status.text())
        self.queue(replacement, "on_resize", replacement.width, replacement.height)
        self.app.processEvents()
        self.assertIsNotNone(self.window.history.find("new.example"))

    def test_old_clipboard_cannot_overwrite_the_new_sessions_clipboard(self):
        old = self.connect("old.example")
        self.queue(old, "on_clipboard", "obsolete clipboard")
        replacement = self.connect("new.example")
        self.app.processEvents()
        self.clipboard.setText.assert_not_called()
        self.queue(replacement, "on_clipboard", "current clipboard")
        self.app.processEvents()
        self.clipboard.setText.assert_called_once_with("current clipboard")

    def test_callbacks_queued_before_manual_disconnect_are_ignored(self):
        old = self.connect("old.example")
        self.queue(old, "on_clipboard", "obsolete clipboard")
        self.queue(old, "on_resize", old.width, old.height)
        self.window.disconnect()
        self.app.processEvents()
        self.clipboard.setText.assert_not_called()
        self.assertIsNone(self.window.client)
        self.assertEqual(self.window.status.text(), "Not connected")
