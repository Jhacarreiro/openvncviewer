"""Window preferences round-trip through a real INI file between windows."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from openvncviewer import ui  # noqa: E402


class WindowPreferencesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = str(self.root / "interface.ini")
        for patcher in (
            mock.patch.object(ui, "KeyboardHook"),
            mock.patch.object(ui, "default_history_path",
                              return_value=self.root / "servers.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def window(self):
        settings = QSettings(self.path, QSettings.IniFormat)
        window = ui.MainWindow(settings=settings)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        return window

    def test_size_sidebar_visibility_and_sort_order_survive_a_restart(self):
        window = self.window()
        window.resize(700, 500)
        self.app.processEvents()
        expected_size = window.size()
        window.history.remember("alpha.example", 5900)
        window.history.remember("zeta.example", 5900)
        window.host_panel.sort_by_last_used.setChecked(True)
        window.host_panel.collapse_btn.click()
        window.close()

        restored = self.window()
        self.assertEqual(restored.size(), expected_size)
        self.assertTrue(restored.host_panel.body.isHidden())
        self.assertFalse(restored.toggle_sidebar_action.isChecked())
        self.assertTrue(restored.host_panel.sort_by_last_used.isChecked())
        first = restored.host_panel.list_widget.item(0).data(Qt.UserRole)
        self.assertEqual(first["host"], "zeta.example")
        with mock.patch.object(ui.ConnectDialog, "exec", return_value=QDialog.Rejected):
            restored.prompt_connect()

    def test_sidebar_reopens_at_its_saved_width_after_a_restart(self):
        window = self.window()
        window.resize(700, 500)
        self.app.processEvents()
        window.splitter.setSizes([280, 416])
        window.splitter.splitterMoved.emit(280, 1)
        expected_width = window.host_panel.width()
        window.host_panel.collapse_btn.click()
        window.close()

        restored = self.window()
        restored.host_panel.expand_btn.click()
        self.app.processEvents()
        self.assertAlmostEqual(restored.host_panel.width(), expected_width, delta=2)

    def test_a_sidebar_dragged_past_half_reopens_at_half(self):
        window = self.window()
        window.resize(1000, 500)
        self.app.processEvents()
        total = sum(window.splitter.sizes())
        window.splitter.setSizes([int(total * 0.8), total - int(total * 0.8)])
        window.splitter.splitterMoved.emit(int(total * 0.8), 1)
        self.assertGreater(window._sidebar_fraction, 0.75,
                           "dragging wider is still allowed")
        window.close()

        restored = self.window()
        self.assertEqual(restored._sidebar_fraction,
                         ui.SIDEBAR_MAX_RESTORED_FRACTION)
        sizes = restored.splitter.sizes()
        self.assertAlmostEqual(sizes[0] / sum(sizes),
                               ui.SIDEBAR_MAX_RESTORED_FRACTION, delta=0.01)

    def test_a_saved_width_under_the_cap_is_kept(self):
        settings = QSettings(self.path, QSettings.IniFormat)
        settings.setValue("sidebar/fraction", 0.3)
        settings.sync()
        self.assertEqual(self.window()._sidebar_fraction, 0.3)

    def test_closing_full_screen_preserves_the_previous_maximized_state(self):
        window = self.window()
        window.showMaximized()
        self.app.processEvents()
        window.fullscreen_action.setChecked(True)
        window.close()

        restored = self.window()
        self.assertTrue(restored.isMaximized())
        self.assertFalse(restored.isFullScreen())
        self.assertTrue(restored.menuBar().isVisible())

    def test_bad_geometry_and_fraction_fall_back_to_a_usable_window(self):
        for fraction in ("not a number", "nan", -1, 2):
            with self.subTest(fraction=fraction):
                settings = QSettings(self.path, QSettings.IniFormat)
                settings.setValue("window/geometry", "broken geometry")
                settings.setValue("sidebar/fraction", fraction)
                settings.sync()
                window = self.window()
                self.assertTrue(window.host_panel.body.isVisible())
                self.assertGreaterEqual(window.host_panel.width(), 200)
                self.assertGreater(window.view.width(), 0)
                window.close()
