# TouchDesk

[日本語](README.ja.md)

Use your Windows PC from a phone **browser only** (no app on the phone), with real multi-touch like a Surface: tap, scroll, pinch.

- Screen: TightVNC + websockify + noVNC, opened in the phone browser.
- Touch: the browser sends finger positions to a small server on the PC, which injects them with the Windows touch injection API (`InjectTouchInput`). Windows sees a real touchscreen.
- While a phone is connected, the primary display drops to 1280x720 so the page is not huge, and it is restored a few seconds after you disconnect.
- A QR code of the phone URL opens automatically.

## Setup (Windows)

1. Install [TightVNC](https://www.tightvnc.com/) (server). In its settings, Access Control tab, enable **Allow loopback connections**.
2. Download [noVNC](https://github.com/novnc/noVNC) and unpack it to `C:\noVNC` (or into a `noVNC` folder next to these files).
3. Install Python 3.9+, then double-click `start_touchdesk.bat`. It installs `websockify` and `segno` and starts everything.
4. Scan the QR code with your phone (same Wi-Fi/LAN).

Options: `python touchdesk.py --help` (resolution, ports, `--no-resize`, `--no-touch`, `--no-qr`).

## Limits

- Windows only. LAN only; the connection is not encrypted. The touch URL contains a secret token, so do not share it.
- Touch injection was tested by hand on one Windows PC by the author. Right-click by long press is not confirmed. Apps running as administrator may not receive touch, and some apps (e.g. Blender) only support limited touch.
- TightVNC, noVNC, websockify and segno are not bundled. They keep their own licenses (GPL, MPL-2.0, LGPL-3.0, BSD).

## Test

```
python -m unittest discover -s tests -v
```

## License

MIT
