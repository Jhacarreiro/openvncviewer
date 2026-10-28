"""Manual checks, opt-in startup checks and safe release-page navigation."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from openvncviewer import ui  # noqa: E402
from openvncviewer.updates import Release  # noqa: E402

RELEASE = Release("99.0.0",
                  "https://github.com/riazhassan-za/openvncviewer/releases/tag/v99.0.0",
                  "<b>Release notes are text</b>")


class FakeChecker(QObject):
    finished = Signal(object, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.started = 0
        self.cancelled = False

    def check(self):
        self.started += 1
        return True

    def cancel(self):
        self.cancelled = True


class UpdateUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings_path = str(self.root / "interface.ini")
        for patcher in (
            mock.patch.object(ui, "KeyboardHook"),
            mock.patch.object(ui, "UpdateChecker", FakeChecker),
            mock.patch.object(ui, "default_history_path",
                              return_value=self.root / "servers.json"),
            mock.patch.object(QApplication, "clipboard", return_value=mock.Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.info = self.patch(QMessageBox, "information")
        self.warning = self.patch(QMessageBox, "warning")
        self.open_url = self.patch(ui.QDesktopServices, "openUrl")

    def patch(self, owner, name):
        patcher = mock.patch.object(owner, name)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def window(self):
        settings = QSettings(self.settings_path, QSettings.IniFormat)
        window = ui.MainWindow(settings=settings)
        self.addCleanup(window.close)
        return window

    def test_default_is_off_and_manual_check_reports_current_then_allows_retry(self):
        window = self.window()
        self.app.processEvents()
        self.assertFalse(window.check_updates_startup_action.isChecked())
        self.assertIsNone(window._update_checker, "startup contacted GitHub without opt-in")
        window.check_updates_action.trigger()
        self.assertFalse(window.check_updates_action.isEnabled())
        window._update_checker.finished.emit(None, "")
        self.assertTrue(window.check_updates_action.isEnabled())
        self.info.assert_called_once()
        self.assertTrue(window.update_notice.isHidden())
        window.check_updates_action.trigger()
        self.assertEqual(window._update_checker.started, 2)

    def test_startup_preference_survives_restart_and_checks_only_once(self):
        window = self.window()
        window.check_updates_startup_action.trigger()
        window.close()
        restored = self.window()
        self.app.processEvents()
        self.assertTrue(restored.check_updates_startup_action.isChecked())
        self.assertEqual(restored._update_checker.started, 1)
        self.app.processEvents()
        self.assertEqual(restored._update_checker.started, 1)

    def test_startup_notice_preserves_the_session_without_a_modal(self):
        window = self.window()
        client = window.client = mock.Mock()
        window.status.setText("Connected to test desktop")
        window.check_for_updates(manual=False)
        window._update_checker.finished.emit(RELEASE, "")
        self.assertFalse(window.update_notice.isHidden())
        self.assertIn("99.0.0", window.update_notice.text())
        self.assertIs(window.client, client)
        client.stop.assert_not_called()
        self.assertEqual(window.status.text(), "Connected to test desktop")
        self.info.assert_not_called()
        self.warning.assert_not_called()
        self.open_url.assert_not_called()

    def test_automatic_errors_are_quiet_and_manual_errors_explain_failure(self):
        window = self.window()
        window.check_for_updates(manual=False)
        window._update_checker.finished.emit(None, "Could not reach GitHub")
        self.warning.assert_not_called()
        window.check_updates_action.trigger()
        window._update_checker.finished.emit(None, "Could not reach GitHub")
        self.warning.assert_called_once()
        self.info.assert_not_called()
        self.assertTrue(window.check_updates_action.isEnabled())

    def answer_dialog(self, button_text, captured):
        def answer():
            message = self.app.activeModalWidget()
            if isinstance(message, QMessageBox):
                captured.append((message.textFormat(), message.text(), message.detailedText()))
                for button in message.buttons():
                    if button.text() == button_text:
                        button.click()
                        return
                message.reject()
        QTimer.singleShot(0, answer)

    def test_manual_notice_shows_plain_notes_and_opens_only_after_confirmation(self):
        window = self.window()
        window.check_updates_action.trigger()
        captured = []
        self.answer_dialog("Open release page", captured)
        window._update_checker.finished.emit(RELEASE, "")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], Qt.PlainText)
        self.assertIn("99.0.0", captured[0][1])
        self.assertEqual(captured[0][2], RELEASE.notes)
        self.assertEqual(self.open_url.call_args.args[0].toString(), RELEASE.url)

    def test_later_leaves_a_clickable_notice_without_opening_a_browser(self):
        window = self.window()
        window.check_updates_action.trigger()
        captured = []
        self.answer_dialog("Later", captured)
        window._update_checker.finished.emit(RELEASE, "")
        self.open_url.assert_not_called()
        self.assertFalse(window.update_notice.isHidden())
        self.answer_dialog("Open release page", captured)
        window.update_notice.click()
        self.open_url.assert_called_once()

    def test_closing_cancels_a_check_and_ignores_late_results(self):
        window = self.window()
        window.check_updates_action.trigger()
        checker = window._update_checker
        window.close()
        self.assertTrue(checker.cancelled)
        checker.finished.emit(RELEASE, "")
        checker.finished.emit(None, "Late error")
        self.assertTrue(window.update_notice.isHidden())
        self.info.assert_not_called()
        self.warning.assert_not_called()

    def test_closing_before_the_queued_startup_check_does_not_start_networking(self):
        settings = QSettings(self.settings_path, QSettings.IniFormat)
        settings.setValue("updates/check_on_startup", True)
        settings.sync()
        window = self.window()
        window.close()
        self.app.processEvents()
        self.assertIsNone(window._update_checker)
