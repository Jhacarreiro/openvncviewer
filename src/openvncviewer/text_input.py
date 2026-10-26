# OpenVNCViewer - a VNC viewer for macOS Screen Sharing with window scaling.
# Copyright (C) 2026 The OpenVNCViewer contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
"""Text insertion via simulated key presses for remote input."""

import threading

from PySide6.QtCore import Signal, QObject


class TextSender(QObject):
    """Sends text to remote host by simulating individual key presses.

    This allows convenient password input and other text entry through
    keyboard simulation rather than clipboard manipulation.
    """

    finished = Signal()
    error = Signal(str)
    progress = Signal(int, int)

    # Delay between key presses in milliseconds to avoid overwhelming the server
    DEFAULT_DELAY_MS = 50

    def __init__(self, client, delay_ms=DEFAULT_DELAY_MS):
        """Initialize the text sender.

        Args:
            client: RFBClient instance for sending key events
            delay_ms: Milliseconds to wait between key presses
        """
        super().__init__()
        self._client = client
        self._delay_ms = delay_ms
        self._thread = None
        self._stop_event = threading.Event()

    def send_text(self, text):
        """Send text by simulating key presses.

        Args:
            text: String to send to the remote host
        """
        if not self._client:
            self.error.emit("No active connection")
            return

        # Windows clipboard line endings represent one Enter, not two.
        self._stop_event.clear()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        total = sum(self._char_to_keysym(char) is not None for char in text)
        self.progress.emit(0, total)
        if not total:
            self.finished.emit()
            return

        # Run in background thread to avoid blocking UI
        self._thread = threading.Thread(
            target=self._send_text_thread,
            args=(text, total),
            daemon=True
        )
        self._thread.start()

    def _send_text_thread(self, text, total):
        """Worker thread: send each character as a key press."""
        try:
            sent = 0
            for char in text:
                if self._stop_event.is_set():
                    break

                keysym = self._char_to_keysym(char)
                if keysym is not None:
                    self._client.send_key(keysym, True)
                    try:
                        self._stop_event.wait(self._delay_ms / 1000.0)
                    finally:
                        # Cancellation during the delay must still release
                        # the current key before any completion is reported.
                        self._client.send_key(keysym, False)
                    sent += 1
                    self.progress.emit(sent, total)
                    if self._stop_event.wait(self._delay_ms / 1000.0):
                        break

            self.finished.emit()
        except Exception as e:
            self.error.emit(f"Error sending text: {str(e)}")

    @staticmethod
    def _char_to_keysym(char):
        """Convert a character to X11 keysym for RFB protocol.

        Handles printable ASCII and common special characters.
        """
        code = ord(char)

        # Printable ASCII characters
        if 32 <= code <= 126:
            return code

        # Common special characters
        special_chars = {
            '\n': 0xFF0D,  # Return
            '\r': 0xFF0D,  # Return
            '\t': 0xFF09,  # Tab
            ' ': 0x20,     # Space
        }

        if char in special_chars:
            return special_chars[char]

        # Other control characters have no key to press
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return None

        # Latin-1 keysyms equal their code point
        if code < 0x100:
            return code

        # Everything else uses the X11 Unicode keysym range
        return 0x01000000 + code

    @classmethod
    def types_literally(cls, text):
        """Whether every character goes out as a key that types just itself.

        Enter and Tab are keys, but they act on the remote rather than typing
        into the field. Inside a password that submits the login half-typed or
        jumps to the next field, and the other control characters would be
        dropped, so the remote would get a different password.
        """
        return all(char not in "\t\n\r" and cls._char_to_keysym(char) is not None
                   for char in text)

    def stop(self):
        """Stop sending text if currently in progress."""
        self._stop_event.set()
