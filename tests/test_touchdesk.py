import base64
import ctypes
import json
import os
import socket
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import touch_server  # noqa: E402
import touchdesk  # noqa: E402


class FakeInjector:
    def __init__(self):
        self.calls = []

    def inject(self, contacts):
        self.calls.append([(s, round(x, 3), round(y, 3), st) for s, x, y, st in contacts])


class TouchSessionTest(unittest.TestCase):
    def setUp(self):
        self.inj = FakeInjector()
        self.slots = {}
        self.s = touch_server.TouchSession(self.inj, self.slots)

    def test_down_move_up(self):
        self.s.handle([{"id": 7, "x": 0.5, "y": 0.25, "s": "d"}])
        self.s.handle([{"id": 7, "x": 0.6, "y": 0.3, "s": "m"}])
        self.s.handle([{"id": 7, "x": 0.6, "y": 0.3, "s": "u"}])
        self.assertEqual(self.inj.calls, [
            [(0, 0.5, 0.25, "d")],
            [(0, 0.6, 0.3, "m")],
            [(0, 0.6, 0.3, "u")],
        ])
        self.assertEqual(self.slots, {})

    def test_two_fingers_and_unmentioned_finger_is_kept(self):
        self.s.handle([{"id": 1, "x": 0.1, "y": 0.1, "s": "d"}])
        self.s.handle([{"id": 1, "x": 0.1, "y": 0.1, "s": "m"}, {"id": 2, "x": 0.9, "y": 0.9, "s": "d"}])
        # 2 本目だけが動いた更新でも、1 本目は位置を保って渡される
        self.s.handle([{"id": 2, "x": 0.8, "y": 0.8, "s": "m"}])
        last = {c[0]: c for c in self.inj.calls[-1]}
        self.assertEqual(last[0], (0, 0.1, 0.1, "m"))
        self.assertEqual(last[1], (1, 0.8, 0.8, "m"))
        # 1 本目を離しても、2 本目は残る
        self.s.handle([{"id": 1, "x": 0.1, "y": 0.1, "s": "u"}, {"id": 2, "x": 0.8, "y": 0.8, "s": "m"}])
        self.assertEqual(sorted(self.slots), [1])

    def test_move_without_down_starts_a_touch(self):
        self.s.handle([{"id": 9, "x": 0.2, "y": 0.2, "s": "m"}])
        self.assertEqual(self.inj.calls[0], [(0, 0.2, 0.2, "d")])

    def test_up_for_unknown_finger_is_ignored(self):
        self.s.handle([{"id": 3, "x": 0.2, "y": 0.2, "s": "u"}])
        self.assertEqual(self.inj.calls and self.inj.calls[-1], [] if self.inj.calls else None)

    def test_release_all_lifts_every_finger(self):
        self.s.handle([{"id": 1, "x": 0.1, "y": 0.1, "s": "d"}, {"id": 2, "x": 0.2, "y": 0.2, "s": "d"}])
        self.s.release_all()
        self.assertEqual({c[3] for c in self.inj.calls[-1]}, {"u"})
        self.assertEqual(self.slots, {})

    def test_more_than_ten_fingers_are_capped(self):
        items = [{"id": i, "x": 0.5, "y": 0.5, "s": "d"} for i in range(15)]
        self.s.handle(items)
        self.assertLessEqual(len(self.slots), touch_server.MAX_CONTACTS)


def ws_frame(text):
    payload = text.encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    assert len(payload) < 126
    return bytes([0x81, 0x80 | len(payload)]) + mask + masked


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def handshake(port, token):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /?token=%s HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (token, key)).encode())
    first = s.recv(300).split(b"\r\n")[0]
    return s, first


class WebSocketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inj = FakeInjector()
        cls.port = free_port()
        touch_server.start_in_thread(cls.port, "secret-token", cls.inj)

    def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_wrong_token_is_rejected(self):
        _, first = handshake(self.port, "wrong")
        self.assertIn(b"403", first)

    def test_touch_frames_reach_injector_and_disconnect_lifts(self):
        before = len(self.inj.calls)
        s, first = handshake(self.port, "secret-token")
        self.assertIn(b"101", first)
        s.sendall(ws_frame(json.dumps([{"id": 1, "x": 0.5, "y": 0.5, "s": "d"}])))
        self.assertTrue(self.wait_for(lambda: len(self.inj.calls) > before))
        self.assertEqual(self.inj.calls[before], [(0, 0.5, 0.5, "d")])
        s.sendall(ws_frame("これはJSONではない"))   # 壊れたデータでも落ちない
        s.close()                                      # 指を離さずに切れた
        self.assertTrue(self.wait_for(lambda: self.inj.calls[-1][0][3] == "u"))


class TcpTableTest(unittest.TestCase):
    def test_counts_established_on_port_only(self):
        def row(state, lport):
            return struct.pack("<6I", state, 0x0100007F, socket.htons(lport), 0x0100007F, socket.htons(50000), 1)
        rows = [row(2, 5900), row(5, 5900), row(5, 5900), row(11, 5900), row(5, 6080), row(5, 5901)]
        buf = struct.pack("<I", len(rows)) + b"".join(rows)
        self.assertEqual(touchdesk.parse_tcp_table(buf, 5900), 2)
        self.assertEqual(touchdesk.parse_tcp_table(buf, 6080), 1)
        self.assertEqual(touchdesk.parse_tcp_table(buf, 9999), 0)


class StructLayoutTest(unittest.TestCase):
    def test_pointer_structs_match_windows_sizes(self):
        self.assertEqual(ctypes.sizeof(touch_server.POINTER_INFO), 96)
        self.assertEqual(ctypes.sizeof(touch_server.POINTER_TOUCH_INFO), 144)

    @unittest.skipUnless(sys.platform == "win32", "DEVMODEW の大きさは Windows でだけ確かめられる")
    def test_devmode_size(self):
        self.assertEqual(ctypes.sizeof(touchdesk.DEVMODEW), 220)


class HelpersTest(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(touchdesk.parse_size("1280x720"), (1280, 720))

    def test_token_is_created_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.txt")
            a = touchdesk.load_token(p)
            self.assertEqual(a, touchdesk.load_token(p))
            self.assertGreaterEqual(len(a), 12)


if __name__ == "__main__":
    unittest.main()
