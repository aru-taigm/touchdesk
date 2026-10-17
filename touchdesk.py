"""TouchDesk: スマホのブラウザから PC を、本物のタッチ操作で使う。

- websockify を起動して、noVNC（画面の配信）を動かす
- VNC（既定 5900 番）に接続が来たら、メイン画面の解像度を --size に変える
- 接続が切れて --grace 秒たったら、元の解像度に戻す
- 解像度の変更は「一時的」なもの（Windows の設定には保存しない）。再起動すれば元に戻る
- スマホの指の動きを、Windows の本物のタッチ入力として注入する（touch_server.py / touch.html）
- スマホで開く URL を QR コードにして、ブラウザに表示する（segno が入っているとき）

使い方:  python touchdesk.py --size 1280x720
"""
import argparse
import atexit
import ctypes
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import webbrowser
from ctypes import wintypes

ENUM_CURRENT_SETTINGS = -1
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
CDS_TEST = 0x00000002
DISP_CHANGE_SUCCESSFUL = 0
CREATE_NO_WINDOW = 0x08000000


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _user32():
    u = ctypes.windll.user32
    u.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
    u.EnumDisplaySettingsW.restype = wintypes.BOOL
    u.ChangeDisplaySettingsW.argtypes = [ctypes.POINTER(DEVMODEW), wintypes.DWORD]
    u.ChangeDisplaySettingsW.restype = wintypes.LONG
    return u


def current_mode():
    u = _user32()
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if not u.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        raise OSError("現在の解像度を取得できませんでした")
    return dm


def set_mode(width, height, bpp, freq):
    """メイン画面を一時的に変更する。成功したら True。"""
    u = _user32()
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    dm.dmPelsWidth = width
    dm.dmPelsHeight = height
    dm.dmBitsPerPel = bpp
    dm.dmDisplayFrequency = freq
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY
    if u.ChangeDisplaySettingsW(ctypes.byref(dm), CDS_TEST) != DISP_CHANGE_SUCCESSFUL:
        return False
    # フラグ 0 = 一時的な変更（レジストリに保存しない）
    return u.ChangeDisplaySettingsW(ctypes.byref(dm), 0) == DISP_CHANGE_SUCCESSFUL


def parse_tcp_table(buf, port):
    """GetExtendedTcpTable の結果から、指定ポートで ESTABLISHED の接続数を数える。"""
    n = struct.unpack_from("<I", buf, 0)[0]
    count = 0
    for i in range(n):
        state, _laddr, lport, _raddr, _rport, _pid = struct.unpack_from("<6I", buf, 4 + 24 * i)
        if state == 5 and socket.ntohs(lport & 0xFFFF) == port:  # 5 = ESTABLISHED
            count += 1
    return count


def count_viewers_native(vnc_port):
    """Windows の API で接続数を数える（別プロセスを起動しないので軽い）。"""
    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)  # 必要な大きさを取得
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0)  # AF_INET, TCP_TABLE_OWNER_PID_ALL
        if ret == 0:
            return parse_tcp_table(buf.raw, vnc_port)
        if ret != 122:  # 122 = バッファ不足。それ以外はエラー
            raise OSError("GetExtendedTcpTable: %d" % ret)
    raise OSError("GetExtendedTcpTable: バッファの確保に失敗")


def count_viewers_powershell(vnc_port):
    """予備の方法。PowerShell で数える（起動が重いので、API が使えないときだけ）。"""
    cmd = (
        "@(Get-NetTCPConnection -LocalPort %d -State Established "
        "-ErrorAction SilentlyContinue).Count" % vnc_port
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        ).stdout.strip()
        return int(out or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None  # 取得失敗。状態は変えない


_native_ok = [True]


def count_viewers(vnc_port):
    """VNC サーバーに接続中のビューア数。取得に失敗したら None（状態は変えない）。"""
    if _native_ok[0]:
        try:
            return count_viewers_native(vnc_port)
        except Exception as e:
            _native_ok[0] = False
            log("接続数の取得を予備の方法に切り替えます: %s" % e)
    return count_viewers_powershell(vnc_port)


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # 実際には送信されない。経路の選択だけ
            return sock.getsockname()[0]
    except OSError:
        return "<PCのIP>"


def load_token(path):
    """タッチ用の合言葉。初回に作って保存し、以後は同じものを使う（URL を固定できる）。"""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
            if token:
                return token
    except OSError:
        pass
    token = secrets.token_urlsafe(12)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def setup_touch(args):
    """タッチ注入サーバーと touch.html を用意する。成功したら URL を返す。"""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import touch_server
    token = load_token(os.path.join(here, "touch_token.txt"))
    shutil.copyfile(os.path.join(here, "touch.html"), os.path.join(args.web, "touch.html"))
    touch_server.start_in_thread(args.touch_port, token)
    url = "http://%s:%d/touch.html?token=%s" % (local_ip(), args.port, token)
    if args.touch_port != 6081:
        url += "&tp=%d" % args.touch_port
    return url, here


QR_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>スマホで読み取る</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
         justify-content: center; gap: 16px; background: #f4f4f4; font-family: sans-serif; color: #222; }}
  .qr {{ background: #fff; padding: 16px; border-radius: 12px; }}
  .qr svg {{ display: block; width: min(70vmin, 420px); height: auto; }}
  code {{ font-size: 13px; word-break: break-all; max-width: 90vw; text-align: center; }}
</style></head><body>
<h2>スマホのカメラで読み取ってください</h2>
<div class="qr">{svg}</div>
<code>{url}</code>
<p style="font-size:13px;color:#666">同じ Wi-Fi につないだスマホで開きます。このページは閉じて大丈夫です。</p>
</body></html>
"""


def show_qr(url, here):
    """URL を QR コードにして、既定のブラウザで開く。segno が無ければ何もしない。"""
    try:
        import segno
    except ImportError:
        log("QR コードを出すには、先に  pip install segno  を実行してください（URL の表示だけ続けます）")
        return
    svg = segno.make(url, error="m").svg_inline(scale=8, border=1, dark="#000", light="#fff")
    path = os.path.join(here, "qr.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(QR_PAGE.format(svg=svg, url=url))
    webbrowser.open("file:///" + path.replace(os.sep, "/"))
    log("QR コードをブラウザに表示しました（%s）" % path)


def parse_size(text):
    w, h = text.lower().split("x")
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="1280x720", help="接続中の解像度（既定 1280x720）")
    ap.add_argument("--grace", type=int, default=15, help="切断後、元に戻すまでの秒数（既定 15）")
    ap.add_argument("--web", default=r"C:\noVNC", help="noVNC のフォルダ")
    ap.add_argument("--port", type=int, default=6080, help="noVNC を開くポート")
    ap.add_argument("--vnc", type=int, default=5900, help="VNC サーバーのポート")
    ap.add_argument("--no-resize", action="store_true", help="解像度を変えず、中継だけ行う")
    ap.add_argument("--no-touch", action="store_true", help="本物のタッチ入力を使わない（noVNC 標準のマウス操作のみ）")
    ap.add_argument("--no-qr", action="store_true", help="起動時に QR コードの画面を開かない")
    ap.add_argument("--touch-port", type=int, default=6081, help="タッチ用ポート（既定 6081）")
    args = ap.parse_args()
    target_w, target_h = parse_size(args.size)

    original = {"mode": None}

    def restore():
        o = original["mode"]
        if o is None:
            return
        if set_mode(o.dmPelsWidth, o.dmPelsHeight, o.dmBitsPerPel, o.dmDisplayFrequency):
            log("解像度を元に戻しました: %dx%d" % (o.dmPelsWidth, o.dmPelsHeight))
        else:
            log("元の解像度に戻せませんでした。Windows の「ディスプレイ設定」で戻してください。")
        original["mode"] = None

    atexit.register(restore)

    proc = subprocess.Popen([
        sys.executable, "-m", "websockify", "--web", args.web,
        str(args.port), "127.0.0.1:%d" % args.vnc,
    ])
    log("noVNC を起動しました: http://%s:%d/vnc.html?resize=scale&autoconnect=1" % (local_ip(), args.port))
    if not args.no_touch:
        try:
            url, here = setup_touch(args)
            log("タッチ操作用（スマホはこちらを開く）: " + url)
            if not args.no_qr:
                show_qr(url, here)
        except Exception as e:  # タッチが使えなくても、画面表示は続ける
            log("タッチ入力を開始できませんでした（マウス操作のみで続けます）: %s" % e)
    if not args.no_resize:
        log("接続中は %dx%d にします（切断 %d 秒後に元へ戻します）" % (target_w, target_h, args.grace))

    last_seen = time.time()
    try:
        while proc.poll() is None:
            time.sleep(2)
            if args.no_resize:
                continue
            n = count_viewers(args.vnc)
            if n is None:
                continue
            now = time.time()
            if n > 0:
                last_seen = now
                if original["mode"] is None:
                    cur = current_mode()
                    if (cur.dmPelsWidth, cur.dmPelsHeight) == (target_w, target_h):
                        continue
                    if set_mode(target_w, target_h, cur.dmBitsPerPel, cur.dmDisplayFrequency):
                        original["mode"] = cur
                        log("接続を検知: %dx%d → %dx%d" % (cur.dmPelsWidth, cur.dmPelsHeight, target_w, target_h))
                    else:
                        log("%dx%d には変更できませんでした（この画面が対応していない解像度です）" % (target_w, target_h))
                        original["mode"] = None
                        args.no_resize = True
            elif original["mode"] is not None and now - last_seen >= args.grace:
                restore()
    except KeyboardInterrupt:
        pass
    finally:
        restore()
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
