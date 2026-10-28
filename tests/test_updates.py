"""Release discovery against independent JSON fixtures and a local HTTP peer."""

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from openvncviewer import updates  # noqa: E402


def published(tag="v99.0.0", **fields):
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "body": "A new release", **fields}


class VersionTest(unittest.TestCase):
    def test_compares_numeric_components_and_never_offers_a_downgrade(self):
        self.assertIsNotNone(updates.newer_release(published("v0.10.0"), "0.9.9"))
        for tag in ("v0.9.9", "v0.10.0"):
            with self.subTest(tag=tag):
                self.assertIsNone(updates.newer_release(published(tag), "0.10.0"))

    def test_drafts_and_prereleases_are_not_offered(self):
        for flag in ("draft", "prerelease"):
            with self.subTest(flag=flag):
                self.assertIsNone(updates.newer_release(published(**{flag: True}), "0.1.0"))

    def test_malformed_or_unrecognized_versions_do_not_mean_up_to_date(self):
        for data in ([], {}, published(None), published("../unexpected"),
                     published("v1.2.3-beta"), published("v١.٢.٣"),
                     published(draft="false")):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    updates.newer_release(data, "0.1.0")

    def test_builds_a_project_link_instead_of_trusting_response_urls(self):
        result = updates.newer_release(published("v1.2.3", html_url="file:///bad.exe"), "0.1.0")
        self.assertEqual(result.url,
                         "https://github.com/riazhassan-za/openvncviewer/releases/tag/v1.2.3")


class NetworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.response = (200, json.dumps(published()).encode(), {})
        self.requests = []
        self.stall = False
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append((self.path, dict(self.headers)))
                if fixture.stall:
                    fixture.release.wait(3)
                code, body, headers = fixture.response
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(body)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    pass  # Timeout/cancellation closes the client side.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        patcher = mock.patch.object(updates, "API_URL",
                                    f"http://127.0.0.1:{self.server.server_port}/release")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.checker = updates.UpdateChecker()
        self.addCleanup(self.checker.cancel)
        self.results = []
        self.checker.finished.connect(lambda release, error: self.results.append((release, error)))

    def close_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), "asynchronous check did not complete")

    def check(self):
        self.checker.check()
        self.wait_for(lambda: self.results)
        return self.results[-1]

    def test_fetches_a_release_without_credentials_and_emits_once(self):
        release, error = self.check()
        self.assertEqual(release.version, "99.0.0")
        self.assertEqual(error, "")
        self.assertEqual(len(self.results), 1)
        path, headers = self.requests[0]
        self.assertEqual(path, "/release")
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertNotIn("authorization", headers)
        self.assertTrue(headers["user-agent"].startswith("OpenVNCViewer/"))

    def test_network_errors_and_bad_json_are_not_reported_as_current(self):
        for status, body in ((403, b"denied"), (429, b"limited"),
                             (404, b"absent"), (200, b"not JSON"),
                             (200, b"[" * 2000 + b"0" + b"]" * 2000)):
            with self.subTest(status=status):
                self.response = status, body, {}
                self.results.clear()
                release, error = self.check()
                self.assertIsNone(release)
                self.assertTrue(error)

    def test_does_not_follow_redirects(self):
        self.response = 302, b"", {"Location": "/unexpected"}
        release, error = self.check()
        self.assertIsNone(release)
        self.assertTrue(error)
        self.assertEqual([path for path, _ in self.requests], ["/release"])

    def test_stalled_requests_time_out_without_blocking_the_event_loop(self):
        self.stall = True
        responsive = []
        with mock.patch.object(updates, "CHECK_TIMEOUT_MS", 75):
            self.checker.check()
            QTimer.singleShot(0, lambda: responsive.append(True))
            self.wait_for(lambda: self.results)
        self.assertTrue(responsive)
        self.assertIn("timed out", self.results[0][1])

    def test_oversized_responses_are_rejected_with_one_completion(self):
        self.response = 200, b"x" * 4096, {}
        with mock.patch.object(updates, "MAX_RESPONSE_BYTES", 1024):
            release, error = self.check()
        self.assertIsNone(release)
        self.assertIn("too large", error)
        self.assertEqual(len(self.results), 1)

    def test_cancel_is_silent_and_a_later_check_can_succeed(self):
        self.stall = True
        self.checker.check()
        self.assertFalse(self.checker.check(), "duplicate check was started")
        self.wait_for(lambda: self.requests)
        self.checker.cancel()
        self.release.set()
        self.stall = False
        release, error = self.check()
        self.assertEqual(release.version, "99.0.0")
        self.assertEqual(error, "")
        self.assertEqual(len(self.results), 1)
