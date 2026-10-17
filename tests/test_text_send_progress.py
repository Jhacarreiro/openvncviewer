"""Text sends report progress, stop cleanly and stay with their own session."""

import os
import socket
import struct
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton  # noqa: E402

from openvncviewer import ui  # noqa: E402
from openvncviewer.rfb import RFBClient  # noqa: E402
from openvncviewer.text_input import TextSender  # noqa: E402


class RecordingClient:
    def __init__(self):
        self.events = []
        self.pressed = threading.Event()

    def send_key(self, keysym, down):
        self.events.append((keysym, down))
        if down:
            self.pressed.set()


class TextSendWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.writer, self.reader = socket.socketpair()
        self.reader.settimeout(3)
        self.addCleanup(self.writer.close)
        self.addCleanup(self.reader.close)
        self.client = RFBClient("localhost", 5900, "", "",
                                lambda *args: None, lambda *args: None,
                                lambda *args: None)
        self.client._sock = self.writer
        self.client._ready = True
        self.events = []
        self.errors = []
        self.pressed = threading.Event()

        def receive():
            try:
                data = bytearray()
                while True:
                    chunk = self.reader.recv(8 - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) == 8:
                        kind, down, padding, keysym = struct.unpack("!BBHI", data)
                        self.assertEqual((kind, padding), (4, 0))
                        self.events.append((keysym, bool(down)))
                        if down:
                            self.pressed.set()
                        data.clear()
                self.assertEqual(data, bytearray(), "partial key event")
            except Exception as exc:
                self.errors.append(exc)

        self.receiver = threading.Thread(target=receive, daemon=True)
        self.receiver.start()

    def finish(self, sender):
        if sender._thread is not None:
            sender._thread.join(3)
            self.assertFalse(sender._thread.is_alive())
        self.writer.shutdown(socket.SHUT_WR)
        self.receiver.join(3)
        self.assertFalse(self.receiver.is_alive())
        self.app.processEvents()
        self.assertEqual(self.errors, [])

    def test_windows_line_endings_and_unicode_reach_the_peer_as_single_keys(self):
        sender = TextSender(self.client, delay_ms=0)
        sender.send_text("A\r\n\u20ac\x00\t")
        self.finish(sender)
        expected = (0x41, 0xFF0D, 0x010020AC, 0xFF09)
        self.assertEqual(self.events, [(key, down) for key in expected
                                      for down in (True, False)])

    def test_progress_counts_only_characters_sent_to_the_peer(self):
        sender = TextSender(self.client, delay_ms=0)
        progress = QSignalSpy(sender.progress)
        sender.send_text("A\r\n\u20ac\x00\t")
        self.finish(sender)
        self.assertEqual([progress.at(i) for i in range(progress.count())],
                         [[0, 4], [1, 4], [2, 4], [3, 4], [4, 4]])

    def test_stopping_during_a_key_press_releases_it_without_typing_the_next(self):
        sender = TextSender(self.client, delay_ms=500)
        sender.send_text("AB")
        self.assertTrue(self.pressed.wait(2), "no key reached the peer")
        sender.stop()
        self.finish(sender)
        self.assertEqual(self.events, [(0x41, True), (0x41, False)])


class TextSendWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QMainWindow()
        self.view = ui.RemoteView()
        self.window.setCentralWidget(self.view)
        self.client = RecordingClient()
        self.view.attach(self.client)
        self.window.show()
        self.addCleanup(self.window.close)
        self.addCleanup(self.view.detach)
        patcher = mock.patch.object(ui.QMessageBox, "warning")
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self, sender):
        if sender._thread is not None:
            sender._thread.join(3)
            self.assertFalse(sender._thread.is_alive())
        self.app.processEvents()

    def test_progress_window_reports_total_and_closes_on_completion(self):
        second_pressed = threading.Event()
        release_second = threading.Event()
        self.addCleanup(release_second.set)
        record = self.client.send_key

        def hold_second(keysym, down):
            record(keysym, down)
            if keysym == 0xFF0D and down:
                second_pressed.set()
                release_second.wait(3)

        self.client.send_key = hold_second
        self.view._send_text_input("A\n\u20ac", 5)
        sender = self.view._text_sender
        progress = self.view._text_progress
        self.assertFalse(progress.isVisible(), "shown before the send ran long")
        self.assertEqual(progress.maximum(), 3)
        self.assertEqual(progress.value(), 0)
        self.assertTrue(second_pressed.wait(2))
        self.app.processEvents()
        self.assertEqual(progress.value(), 1)
        self.assertIn("1 of 3", progress.labelText())
        QTest.qWait(ui.TEXT_PROGRESS_DELAY_MS + 200)
        self.assertTrue(progress.isVisible(), "a long send hid its progress")
        release_second.set()
        self.finish(sender)
        self.assertIsNone(self.view._text_sender)
        self.assertIsNone(self.view._text_progress)
        self.warning.assert_not_called()

    def test_a_short_send_never_shows_its_progress_window(self):
        shown = []

        class Recording(ui.QProgressDialog):
            def showEvent(self, event):
                shown.append(True)
                super().showEvent(event)

        with mock.patch.object(ui, "QProgressDialog", Recording):
            self.view._send_text_input("AB", 5)
            self.finish(self.view._text_sender)
            QTest.qWait(ui.TEXT_PROGRESS_DELAY_MS + 200)
        self.assertEqual(self.client.events, [(0x41, True), (0x41, False),
                                              (0x42, True), (0x42, False)])
        self.assertEqual(shown, [])

    def test_asking_again_during_a_send_shows_its_progress_at_once(self):
        self.view._send_text_input("AB", 500)
        sender = self.view._text_sender
        self.assertTrue(self.client.pressed.wait(2))
        self.view._prompt_text_input()
        self.assertTrue(self.view._text_progress.isVisible())
        self.view._text_progress.findChild(QPushButton).click()
        self.finish(sender)

    def test_stop_button_interrupts_the_send_and_releases_the_current_key(self):
        self.view._send_text_input("AB", 500)
        sender = self.view._text_sender
        self.assertTrue(self.client.pressed.wait(2))
        self.view._text_progress.findChild(QPushButton).click()
        self.finish(sender)
        self.assertEqual(self.client.events, [(0x41, True), (0x41, False)])
        self.assertIsNone(self.view._text_sender)
        self.assertIsNone(self.view._text_progress)

    def test_changing_clients_cancels_the_old_send_without_affecting_the_new_one(self):
        self.view._send_text_input("AB", 500)
        old = self.view._text_sender
        self.assertTrue(self.client.pressed.wait(2))
        self.view.detach()
        replacement = RecordingClient()
        allow_current = threading.Event()
        self.addCleanup(allow_current.set)
        record = replacement.send_key

        def hold_current(keysym, down):
            record(keysym, down)
            if down:
                allow_current.wait(3)

        replacement.send_key = hold_current
        self.view.attach(replacement)
        self.view._send_text_input("C", 5)
        current = self.view._text_sender
        old._thread.join(3)
        self.assertFalse(old._thread.is_alive())
        self.app.processEvents()
        self.assertIs(self.view._text_sender, current)
        allow_current.set()
        self.finish(current)
        self.assertEqual(self.client.events, [(0x41, True), (0x41, False)])
        self.assertEqual(replacement.events, [(0x43, True), (0x43, False)])
        self.warning.assert_not_called()

    def test_a_second_send_cannot_overlap_the_current_one(self):
        self.view._send_text_input("AB", 500)
        sender = self.view._text_sender
        self.assertTrue(self.client.pressed.wait(2))
        self.view._send_text_input("C", 0)
        self.assertIs(self.view._text_sender, sender)
        self.view._text_progress.findChild(QPushButton).click()
        self.finish(sender)
        self.assertEqual(self.client.events, [(0x41, True), (0x41, False)])

    def test_a_network_error_closes_progress_and_reports_the_failure(self):
        self.client.send_key = mock.Mock(side_effect=OSError("send failed"))
        self.view._send_text_input("A", 5)
        self.finish(self.view._text_sender)
        self.assertIsNone(self.view._text_sender)
        self.assertIsNone(self.view._text_progress)
        self.warning.assert_called_once()
        self.assertIn("send failed", self.warning.call_args.args[2])
