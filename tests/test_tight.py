"""Tight encoding, as TightVNC sends it, decoded against an independent encoder.

The encoder here is written from the protocol description, not from the
decoder, and every rectangle is compared pixel for pixel against the image it
was made from. JPEG is lossy, so it is compared within a tolerance instead.
"""

import io
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from openvncviewer import ui  # noqa: E402
from openvncviewer.history import ServerHistory, _clean_session  # noqa: E402
from openvncviewer.rfb import (ENC_TIGHT, ENC_ZRLE, RFBClient,  # noqa: E402
                               RFBError)

WIDTH, HEIGHT = 64, 48


def compact_length(n):
    """Tight's 1-3 byte length: 7 bits a byte, the third byte all 8."""
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([n & 0x7F | 0x80, n >> 7])
    return bytes([n & 0x7F | 0x80, (n >> 7) & 0x7F | 0x80, n >> 14])


class TightEncoder:
    """Builds Tight rectangle bodies the way a server does, four zlib streams
    and all. Each stream persists across rectangles until reset."""

    def __init__(self):
        self.streams = [zlib.compressobj() for _ in range(4)]

    def data(self, stream, raw):
        if len(raw) < 12:
            return raw
        packer = self.streams[stream]
        packed = packer.compress(raw) + packer.flush(zlib.Z_SYNC_FLUSH)
        return compact_length(len(packed)) + packed

    def fill(self, rgb):
        return bytes([0x80]) + bytes(rgb)

    def copy(self, pixels, stream=0, reset=0):
        raw = b"".join(bytes(p) for p in pixels)
        return bytes([stream << 4 | reset]) + self.data(stream, raw)

    def palette(self, pixels, w, h, stream=1):
        colours = sorted(set(pixels))
        index = {c: i for i, c in enumerate(colours)}
        if len(colours) == 2:
            rows = bytearray()
            for y in range(h):
                bits = [index[pixels[y * w + x]] for x in range(w)]
                bits += [0] * (-len(bits) % 8)
                for i in range(0, len(bits), 8):
                    rows.append(int("".join(map(str, bits[i:i + 8])), 2))
            raw = bytes(rows)
        else:
            raw = bytes(index[p] for p in pixels)
        return (bytes([(0x4 | stream) << 4, 1, len(colours) - 1])
                + b"".join(bytes(c) for c in colours) + self.data(stream, raw))

    def gradient(self, pixels, w, h, stream=2):
        raw = bytearray()
        for y in range(h):
            for x in range(w):
                for c in range(3):
                    left = pixels[y * w + x - 1][c] if x else 0
                    up = pixels[(y - 1) * w + x][c] if y else 0
                    corner = pixels[(y - 1) * w + x - 1][c] if x and y else 0
                    predicted = min(max(left + up - corner, 0), 255) if x else up
                    raw.append((pixels[y * w + x][c] - predicted) & 0xFF)
        return bytes([(0x4 | stream) << 4, 2]) + self.data(stream, bytes(raw))

    @staticmethod
    def jpeg(data):
        return bytes([0x90]) + compact_length(len(data)) + data


def bgrx(pixels):
    return b"".join(bytes((b, g, r, 0xFF)) for r, g, b in pixels)


def photo(w, h):
    """Smooth, many-coloured content - the kind Tight sends as JPEG."""
    return [((x * 255) // max(w - 1, 1), (y * 255) // max(h - 1, 1),
             (x * y) % 256) for y in range(h) for x in range(w)]


def jpeg_of(pixels, w, h, quality=95):
    image = QImage(w, h, QImage.Format_RGB32)
    for i, (r, g, b) in enumerate(pixels):
        image.setPixelColor(i % w, i // w, QColor(r, g, b))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPEG", quality)
    return bytes(buffer.data())


class TightTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_client(self, jpeg_quality=8):
        client = RFBClient("tight.example", 5900, "", "",
                           on_resize=lambda *a: None,
                           on_damage=lambda *a: None,
                           on_disconnect=lambda *a: None,
                           jpeg_quality=jpeg_quality,
                           decode_jpeg=ui.decode_jpeg)
        client._resize(WIDTH, HEIGHT)
        return client

    def decode(self, client, x, y, w, h, body):
        client._reader = io.BytesIO(body)
        client._decode_tight(x, y, w, h)
        self.assertEqual(client._reader.read(), b"",
                         "decoder left part of the rectangle unread")

    def region(self, client, x, y, w, h):
        rows = (client.framebuffer[((y + r) * WIDTH + x) * 4:
                                   ((y + r) * WIDTH + x + w) * 4]
                for r in range(h))
        return b"".join(rows)


class TightDecodeTest(TightTestCase):
    def test_fill(self):
        client = self.make_client()
        self.decode(client, 3, 4, 10, 6, TightEncoder().fill((200, 100, 50)))
        self.assertEqual(self.region(client, 3, 4, 10, 6),
                         bgrx([(200, 100, 50)] * 60))

    def test_copy(self):
        pixels = photo(20, 10)
        client = self.make_client()
        self.decode(client, 0, 0, 20, 10, TightEncoder().copy(pixels))
        self.assertEqual(self.region(client, 0, 0, 20, 10), bgrx(pixels))

    def test_two_colour_bitmap_with_padded_rows(self):
        """Text is mostly this. 13 is not a multiple of 8, so every row pads."""
        pixels = [(255, 255, 255) if (x + y) % 3 else (10, 20, 30)
                  for y in range(9) for x in range(13)]
        client = self.make_client()
        self.decode(client, 5, 5, 13, 9, TightEncoder().palette(pixels, 13, 9))
        self.assertEqual(self.region(client, 5, 5, 13, 9), bgrx(pixels))

    def test_many_colour_palette(self):
        colours = [(i * 30, 255 - i * 30, i * 7) for i in range(8)]
        pixels = [colours[(x * y) % 8] for y in range(12) for x in range(16)]
        client = self.make_client()
        self.decode(client, 0, 0, 16, 12, TightEncoder().palette(pixels, 16, 12))
        self.assertEqual(self.region(client, 0, 0, 16, 12), bgrx(pixels))

    def test_gradient(self):
        pixels = photo(17, 11)
        client = self.make_client()
        self.decode(client, 2, 3, 17, 11,
                    TightEncoder().gradient(pixels, 17, 11))
        self.assertEqual(self.region(client, 2, 3, 17, 11), bgrx(pixels))

    def test_gradient_clamps_its_prediction(self):
        """left + up - corner runs past 0-255 at sharp edges."""
        pixels = [(255, 0, 255) if (x + y) % 2 else (0, 255, 0)
                  for y in range(6) for x in range(6)]
        client = self.make_client()
        self.decode(client, 0, 0, 6, 6, TightEncoder().gradient(pixels, 6, 6))
        self.assertEqual(self.region(client, 0, 0, 6, 6), bgrx(pixels))

    def test_jpeg(self):
        pixels = photo(32, 24)
        client = self.make_client()
        self.decode(client, 8, 8, 32, 24,
                    TightEncoder.jpeg(jpeg_of(pixels, 32, 24)))
        actual = self.region(client, 8, 8, 32, 24)
        expected = bgrx(pixels)
        worst = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLess(worst, 40, "JPEG decoded far from its source")
        self.assertEqual(actual[3::4], b"\xff" * (32 * 24))

    def test_short_data_is_sent_without_zlib(self):
        """Under 12 bytes Tight sends the pixels as they are, with no length."""
        pixels = [(1, 2, 3), (4, 5, 6), (7, 8, 9)]  # 9 bytes
        body = TightEncoder().copy(pixels)
        self.assertEqual(len(body), 1 + 9)
        client = self.make_client()
        self.decode(client, 0, 0, 3, 1, body)
        self.assertEqual(self.region(client, 0, 0, 3, 1), bgrx(pixels))

    def test_a_zlib_stream_carries_on_across_rectangles(self):
        """Servers keep each stream's dictionary between rectangles; a decoder
        that restarted it would fail on the second one."""
        encoder = TightEncoder()
        client = self.make_client()
        first, second = photo(16, 8), photo(8, 16)
        self.decode(client, 0, 0, 16, 8, encoder.copy(first))
        self.decode(client, 20, 0, 8, 16, encoder.copy(second))
        self.assertEqual(self.region(client, 0, 0, 16, 8), bgrx(first))
        self.assertEqual(self.region(client, 20, 0, 8, 16), bgrx(second))

    def test_a_reset_starts_the_stream_again(self):
        encoder = TightEncoder()
        client = self.make_client()
        self.decode(client, 0, 0, 16, 8, encoder.copy(photo(16, 8)))
        encoder.streams[0] = zlib.compressobj()
        pixels = photo(8, 8)
        self.decode(client, 0, 0, 8, 8, encoder.copy(pixels, reset=0x1))
        self.assertEqual(self.region(client, 0, 0, 8, 8), bgrx(pixels))

    def test_compact_lengths_of_every_size(self):
        for n in (0, 127, 128, 16383, 16384, 4194303):
            with self.subTest(n=n):
                client = self.make_client()
                client._reader = io.BytesIO(compact_length(n))
                self.assertEqual(client._read_compact_length(), n)

    def test_a_whole_update_through_the_message_loop(self):
        encoder = TightEncoder()
        pixels = photo(WIDTH, HEIGHT)
        rects = [(0, 0, WIDTH, HEIGHT, encoder.copy(pixels)),
                 (4, 4, 8, 8, encoder.fill((9, 9, 9)))]
        stream = struct.pack("!xH", len(rects)) + b"".join(
            struct.pack("!HHHHi", x, y, w, h, ENC_TIGHT) + body
            for x, y, w, h, body in rects)
        damage = []
        client = self.make_client()
        client._on_damage = lambda *bounds: damage.append(bounds)
        client._reader = io.BytesIO(stream)
        client._handle_framebuffer_update()
        expected = bytearray(bgrx(pixels))
        for row in range(4, 12):
            expected[(row * WIDTH + 4) * 4:(row * WIDTH + 12) * 4] = \
                bgrx([(9, 9, 9)] * 8)
        self.assertEqual(bytes(client.framebuffer), bytes(expected))
        self.assertEqual(damage, [(0, 0, WIDTH, HEIGHT)])


class TightNegotiationTest(TightTestCase):
    def encodings(self, client):
        sent = []
        client._send = sent.append
        client._set_encodings()
        count = struct.unpack("!xxH", sent[0][:4])[0]
        return list(struct.unpack(f"!{count}i", sent[0][4:]))

    def test_a_quality_asks_for_tight_first_with_jpeg(self):
        self.assertEqual(self.encodings(self.make_client(jpeg_quality=8)),
                         [ENC_TIGHT, ENC_ZRLE, 1, 0, -223, -32 + 8])

    def test_lossless_asks_for_exactly_what_it_did_before(self):
        self.assertEqual(self.encodings(self.make_client(jpeg_quality=None)),
                         [ENC_ZRLE, 1, 0, -223])

    def test_without_a_jpeg_decoder_tight_is_not_offered(self):
        client = RFBClient("h", 5900, "", "", lambda *a: None,
                           lambda *a: None, lambda *a: None, jpeg_quality=8)
        self.assertEqual(self.encodings(client), [ENC_ZRLE, 1, 0, -223])

    def test_tight_from_a_server_not_asked_for_it_is_refused(self):
        client = self.make_client(jpeg_quality=None)
        client._reader = io.BytesIO(
            struct.pack("!xH", 1) + struct.pack("!HHHHi", 0, 0, 4, 4, ENC_TIGHT)
            + TightEncoder().fill((1, 2, 3)))
        with self.assertRaises(RFBError) as caught:
            client._handle_framebuffer_update()
        self.assertIn("unrequested", str(caught.exception))


class TightHostileTest(TightTestCase):
    """Everything here is something a server can send."""

    def refused(self, w, h, body, words):
        client = self.make_client()
        before = bytes(client.framebuffer)
        client._reader = io.BytesIO(body)
        with self.assertRaises(RFBError) as caught:
            client._decode_tight(0, 0, w, h)
        self.assertIn(words, str(caught.exception))
        self.assertEqual(bytes(client.framebuffer), before,
                         "a refused rectangle still painted")
        self.assertEqual(len(client.framebuffer), WIDTH * HEIGHT * 4)

    def test_a_compression_bomb_stops_at_the_rectangle(self):
        bomb = zlib.compress(b"\x00" * 50_000_000, 9)
        self.refused(8, 8, bytes([0x00]) + compact_length(len(bomb)) + bomb,
                     "expanded past")

    def test_too_little_data_is_refused(self):
        packed = zlib.compress(b"\x01" * 30)
        self.refused(8, 8, bytes([0x00]) + compact_length(len(packed)) + packed,
                     "only 30 bytes")

    def test_garbage_zlib_is_a_protocol_error(self):
        self.refused(8, 8, bytes([0x00]) + compact_length(5) + b"junk!",
                     "malformed Tight zlib")

    def test_an_unknown_compression_type(self):
        self.refused(8, 8, bytes([0xA0]), "compression type")

    def test_an_unknown_filter(self):
        self.refused(8, 8, bytes([0x40, 7]), "filter")

    def test_a_palette_index_past_the_palette(self):
        raw = bytes([0, 1, 2, 5]) * 4  # index 5 into three colours
        self.refused(4, 4, bytes([0x40, 1, 2]) + bytes(9)
                     + compact_length(len(zlib.compress(raw)))
                     + zlib.compress(raw), "palette index 5")

    def test_a_one_colour_palette(self):
        self.refused(4, 4, bytes([0x40, 1, 0, 1, 2, 3]), "fewer than two")

    def test_a_rectangle_off_the_framebuffer(self):
        client = self.make_client()
        client._reader = io.BytesIO(TightEncoder().fill((1, 2, 3)))
        with self.assertRaises(RFBError):
            client._decode_tight(WIDTH - 2, 0, 8, 8)
        self.assertEqual(len(client.framebuffer), WIDTH * HEIGHT * 4)

    def test_a_jpeg_of_the_wrong_size_is_refused_before_decoding(self):
        """Whatever a JPEG claims, it may only fill its own rectangle."""
        big = jpeg_of(photo(40, 40), 40, 40)
        self.refused(8, 8, TightEncoder.jpeg(big), "40x40, not the 8x8")

    def test_a_corrupt_jpeg_is_refused(self):
        self.refused(8, 8, TightEncoder.jpeg(b"\xff\xd8 not really a jpeg"),
                     "JPEG")

    def test_a_truncated_rectangle_ends_the_session_cleanly(self):
        body = TightEncoder().copy(photo(16, 16))
        self.refused(16, 16, body[:len(body) // 2], "connection closed")


class PictureQualityTest(TightTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = ServerHistory(Path(directory.name) / "servers.json")

    def test_new_and_old_servers_default_to_high(self):
        self.history.remember("old.example", 5900)
        self.assertEqual(self.history.find("old.example")["picture_quality"],
                         "high")
        dialog = ui.ConnectDialog(history=self.history)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.picture_quality.currentData(), "high")

    def test_a_saved_choice_survives_and_a_bad_one_falls_back_to_high(self):
        self.history.remember("pc.example", 5900, picture_quality="low")
        reloaded = ServerHistory(self.history.path)
        self.assertEqual(reloaded.find("pc.example")["picture_quality"], "low")
        for bad in ("ultra", 7, None, ["high"]):
            with self.subTest(value=bad):
                self.assertEqual(
                    _clean_session({"picture_quality": bad})["picture_quality"],
                    "high")

    def test_selecting_a_saved_server_restores_its_quality(self):
        self.history.remember("pc.example", 5900, picture_quality="lossless")
        dialog = ui.ConnectDialog(history=self.history)
        self.addCleanup(dialog.close)
        dialog.host.setEditText("pc.example")
        self.assertEqual(dialog.picture_quality.currentData(), "lossless")

    def test_each_choice_reaches_the_client(self):
        created = []

        class Capture:
            def __init__(self, *args, **kwargs):
                created.append(kwargs)

            def start(self):
                pass

            def stop(self):
                pass

        with mock.patch.object(ui, "RFBClient", Capture), \
                mock.patch.object(ui, "KeyboardHook"), \
                mock.patch.object(ui, "default_history_path",
                                  return_value=self.history.path):
            window = ui.MainWindow()
            self.addCleanup(window.close)
            for choice, quality in ui.JPEG_QUALITY.items():
                with self.subTest(choice=choice):
                    window.connect_to("pc.example", 5900, "", "pw",
                                      picture_quality=choice)
                    self.assertEqual(created[-1]["jpeg_quality"], quality)
                    self.assertIs(created[-1]["decode_jpeg"], ui.decode_jpeg)


if __name__ == "__main__":
    unittest.main()
