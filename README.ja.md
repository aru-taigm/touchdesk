# TouchDesk

[English](README.md)

スマホの**ブラウザだけ**（スマホ側アプリ不要）で Windows PC の画面を見て、Surface のような本物のマルチタッチ（タップ・スクロール・ピンチ）で操作できます。

- 画面: TightVNC + websockify + noVNC をスマホのブラウザで表示します。
- タッチ: ブラウザが指の位置を PC 上の小さなサーバーに送り、Windows のタッチ注入 API（`InjectTouchInput`）で入力します。Windows からは本物のタッチ画面に見えます。
- スマホ接続中はメイン画面を 1280x720 に下げ、切断の数秒後に元へ戻します。
- スマホ用 URL の QR コードが自動で開きます。

## セットアップ（Windows）

1. [TightVNC](https://www.tightvnc.com/)（サーバー）を入れます。設定の「Access Control」タブで **Allow loopback connections** をオンにします。
2. [noVNC](https://github.com/novnc/noVNC) を `C:\noVNC` に展開します（このフォルダの隣の `noVNC` でも可）。
3. Python 3.9 以上を入れ、`start_touchdesk.bat` をダブルクリックします。`websockify` と `segno` を入れて全部起動します。
4. 同じ Wi-Fi/LAN のスマホで QR コードを読み取ります。

オプション: `python touchdesk.py --help`（解像度、ポート、`--no-resize`、`--no-touch`、`--no-qr`）。

## 制限

- Windows 専用、LAN 内のみ。通信は暗号化されません。タッチ用 URL には秘密のトークンが入っているので共有しないでください。
- タッチ入力は作者が Windows 1台で手動確認しました。長押しの右クリックは確認できていません。管理者権限で動くアプリはタッチを受け取れないことがあり、Blender など一部アプリはタッチ対応が限定的です。
- TightVNC・noVNC・websockify・segno は同梱しません。それぞれのライセンス（GPL、MPL-2.0、LGPL-3.0、BSD）に従います。

## テスト

```
python -m unittest discover -s tests -v
```

## ライセンス

MIT
