"""スマホの指の動きを、Windows の本物のタッチ入力として PC に注入する小さなサーバー。

touch.html（スマホのブラウザ）→ WebSocket → このサーバー → InjectTouchInput（Windows 8 以降）
標準ライブラリだけで動く。合言葉（token）が合わない接続は受け付けない。
"""
import asyncio
import base64
import ctypes
import hashlib
import hmac
import json
import struct
import threading
from urllib.parse import parse_qs, urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PT_TOUCH = 2
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
TOUCH_MASK_CONTACTAREA = 0x1
TOUCH_MASK_PRESSURE = 0x4
MAX_CONTACTS = 10
MAX_MESSAGE = 65536

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", ctypes.c_void_p),
        ("hwndTarget", ctypes.c_void_p),
        ("ptPixelLocation", POINT),
        ("ptHimetricLocation", POINT),
        ("ptPixelLocationRaw", POINT),
        ("ptHimetricLocationRaw", POINT),
        ("dwTime", ctypes.c_uint32),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", ctypes.c_uint32),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_uint32),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", RECT),
        ("rcContactRaw", RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


def log(msg):
    import time
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


class WindowsInjector:
    """InjectTouchInput で本物のタッチを入れる。座標は 0〜1（画面全体に対する割合）。"""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        try:
            self.user32.SetProcessDPIAware()  # 物理ピクセルで座標を扱う
        except Exception:
            pass
        self.user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self.user32.InitializeTouchInjection.restype = ctypes.c_int32
        self.user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
        self.user32.InjectTouchInput.restype = ctypes.c_int32
        if not self.user32.InitializeTouchInjection(MAX_CONTACTS, 1):  # 1 = 既定のフィードバック
            raise OSError("タッチ注入を初期化できませんでした（Windows 8 以降が必要です）")
        self._warned = False

    def screen_rect(self):
        g = self.user32.GetSystemMetrics
        return g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN), g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN)

    def inject(self, contacts):
        """contacts: [(slot, nx, ny, 'd'|'m'|'u')]。生きている接触は毎回すべて渡す。"""
        if not contacts:
            return
        vx, vy, vw, vh = self.screen_rect()
        arr = (POINTER_TOUCH_INFO * len(contacts))()
        for i, (slot, nx, ny, state) in enumerate(contacts):
            px = vx + int(round(min(max(nx, 0.0), 1.0) * (vw - 1)))
            py = vy + int(round(min(max(ny, 0.0), 1.0) * (vh - 1)))
            c = arr[i]
            c.pointerInfo.pointerType = PT_TOUCH
            c.pointerInfo.pointerId = slot
            if state == "d":
                flags = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            elif state == "u":
                flags = POINTER_FLAG_UP
            else:
                flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            c.pointerInfo.pointerFlags = flags
            c.pointerInfo.ptPixelLocation.x = px
            c.pointerInfo.ptPixelLocation.y = py
            c.touchFlags = 0
            c.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_PRESSURE
            c.rcContact.left, c.rcContact.right = px - 2, px + 2
            c.rcContact.top, c.rcContact.bottom = py - 2, py + 2
            c.orientation = 90
            c.pressure = 0 if state == "u" else 32000
        if not self.user32.InjectTouchInput(len(contacts), arr) and not self._warned:
            self._warned = True
            log("タッチ入力を注入できませんでした（エラー %d）。管理者権限のアプリには届かないことがあります。"
                % ctypes.windll.kernel32.GetLastError())


class TouchSession:
    """1 つの接続ぶんの指の状態。接続が切れたら、残っている指をすべて離す。"""

    def __init__(self, injector, shared_slots):
        self.injector = injector
        self.slots = shared_slots          # slot -> (nx, ny)（全接続で共有）
        self.ids = {}                      # ブラウザ側の指 id -> slot

    def _alloc(self, tid):
        used = set(self.slots)
        for s in range(MAX_CONTACTS):
            if s not in used:
                self.ids[tid] = s
                self.slots[s] = (0.0, 0.0)
                return s
        return None

    def handle(self, items):
        frame = {}
        lifted = []
        for it in items[:MAX_CONTACTS]:
            tid, state = it.get("id"), it.get("s")
            x, y = float(it.get("x", 0)), float(it.get("y", 0))
            if state == "d" or (state == "m" and tid not in self.ids):
                if tid in self.ids:  # 二重の down は更新として扱う
                    slot, st = self.ids[tid], "m"
                else:
                    slot, st = self._alloc(tid), "d"
                if slot is None:
                    continue
            elif tid in self.ids:
                slot, st = self.ids[tid], state if state in ("m", "u") else "m"
            else:
                continue
            self.slots[slot] = (x, y)
            frame[slot] = (slot, x, y, st)
            if st == "u":
                lifted.append((tid, slot))
        # 今回触れていない生きた指も、位置を保ったまま更新として渡す
        mine = set(self.ids.values())
        for slot in mine:
            if slot not in frame:
                x, y = self.slots[slot]
                frame[slot] = (slot, x, y, "m")
        self.injector.inject(list(frame.values()))
        for tid, slot in lifted:
            self.ids.pop(tid, None)
            self.slots.pop(slot, None)

    def release_all(self):
        if not self.ids:
            return
        contacts = []
        for tid, slot in list(self.ids.items()):
            x, y = self.slots.get(slot, (0.0, 0.0))
            contacts.append((slot, x, y, "u"))
        self.injector.inject(contacts)
        for tid, slot in list(self.ids.items()):
            self.slots.pop(slot, None)
        self.ids.clear()


class TouchServer:
    def __init__(self, token, injector):
        self.token = token
        self.injector = injector
        self.slots = {}

    async def _read_frame(self, reader):
        b1, b2 = await reader.readexactly(2)
        op, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", await reader.readexactly(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", await reader.readexactly(8))[0]
        if ln > MAX_MESSAGE:
            raise ValueError("too large")
        mask = await reader.readexactly(4) if masked else b""
        data = await reader.readexactly(ln) if ln else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return op, data

    @staticmethod
    def _frame(op, payload=b""):
        return bytes([0x80 | op, len(payload)]) + payload  # 125 バイト以下のみ

    async def handle(self, reader, writer):
        session = TouchSession(self.injector, self.slots)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin1").split("\r\n")
            parts = lines[0].split(" ")
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            token = parse_qs(urlparse(parts[1]).query).get("token", [""])[0]
            key = headers.get("sec-websocket-key")
            if not key or not hmac.compare_digest(token, self.token):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            await writer.drain()
            while True:
                op, data = await self._read_frame(reader)
                if op == 8:
                    break
                if op == 9:
                    writer.write(self._frame(10, data[:125]))
                    await writer.drain()
                elif op == 1:
                    try:
                        items = json.loads(data.decode("utf-8"))
                        if isinstance(items, list):
                            session.handle([i for i in items if isinstance(i, dict)])
                    except (ValueError, TypeError):
                        pass
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            try:
                session.release_all()
            finally:
                writer.close()


def start_in_thread(port, token, injector=None):
    """別スレッドでサーバーを起動する。起動できたら True。"""
    if injector is None:
        injector = WindowsInjector()
    server = TouchServer(token, injector)
    ready = threading.Event()
    result = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            srv = loop.run_until_complete(asyncio.start_server(server.handle, "0.0.0.0", port))
            result["ok"] = True
        except OSError as e:
            result["ok"] = False
            result["err"] = e
        ready.set()
        if result["ok"]:
            loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    if not result.get("ok"):
        raise OSError("タッチ用ポート %d を開けませんでした: %s" % (port, result.get("err")))
    return server
