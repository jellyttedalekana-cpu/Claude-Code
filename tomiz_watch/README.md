# 在庫監視 → Gmail 下書き

ログインが必要な EC サイトの商品ページを定期的に見て、**在庫切れ → 在庫あり**に変わったら
Gmail に下書きを1通作る。macOS の launchd から動かす想定。標準ライブラリだけで動く（pip 不要）。

対象例: `https://b2b.tomiz.com/item/00195014`

## 仕組み

1. ブラウザから書き出したログイン済み Cookie を使ってページを取得する
2. ページ本文（`<script>` は除く）に在庫ありの文言／在庫切れの文言があるかを見て判定する
3. 前回の状態を `state.json` に覚えておき、**状態が変わったときだけ**通知する
4. 通知は IMAP でGmail の下書きフォルダに1通置く（送信はしない）

### 通知が出る条件

| 状況 | 動き |
|---|---|
| 在庫切れ／判定不能 → 在庫あり | 「[在庫復活]」の下書きを作る |
| ログイン画面に飛ばされた | 「ログインし直しが必要」の下書きを作る（12時間に1回まで） |
| 在庫ありとも切れとも判定できない | 「判定できませんでした」の下書きを作り、HTML を `snapshots/` に保存する |
| 状態が前回と同じ | 何もしない（ログだけ） |

判定不能を黙って握りつぶさないのが肝。握りつぶすと「ずっと在庫切れ」に見えて取りこぼす。

## セットアップ

### 1. 置く

```bash
mkdir -p ~/tomiz-watch
cp watch.py config.example.json ~/tomiz-watch/
cd ~/tomiz-watch
cp config.example.json config.json
```

### 2. Cookie を書き出す

Chrome なら拡張（"Get cookies.txt LOCALLY" など）で **Netscape 形式**の `cookies.txt` を書き出す。

1. ブラウザで b2b.tomiz.com にログインする
2. 商品ページを開いた状態で拡張から書き出す
3. `~/tomiz-watch/cookies.txt` に置く

```bash
chmod 600 ~/tomiz-watch/cookies.txt
```

Cookie には有効期限がある。切れたら「ログインし直しが必要」の下書きが来るので、書き出し直す。

### 3. Gmail のアプリパスワード

2段階認証を有効にしたうえで https://myaccount.google.com/apppasswords で16桁を発行する。
`config.json` に直接書いてもいいが、**環境変数のほうが安全**:

```bash
export TOMIZ_WATCH_GMAIL_APP_PASSWORD='xxxxxxxxxxxxxxxx'
```

`config.json` の `gmail.address` に自分のアドレスを入れる。

### 4. 動作確認

```bash
cd ~/tomiz-watch

# 下書きが作れるか
python3 watch.py --test-draft

# ページ中の在庫らしき文言を並べる（判定の調整用）
python3 watch.py --probe

# 今の判定を見る（状態も通知も変えない）
python3 watch.py --status
```

`--probe` に何も出ないなら、ログインできていないか、在庫表示が JavaScript で描かれている。
出てきた文言に合わせて `config.json` の `in_stock_markers` / `out_of_stock_markers` を直す。

### 5. 定期実行（launchd）

`com.tomizwatch.plist` の `YOUR_NAME` とアプリパスワードを書き換えて:

```bash
cp com.tomizwatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tomizwatch.plist

# すぐ1回走らせる
launchctl start com.tomizwatch

# 止める
launchctl unload ~/Library/LaunchAgents/com.tomizwatch.plist
```

間隔は `StartInterval`（秒）。既定は30分。相手のサイトに迷惑なので、これより短くしない。

## 注意

- 在庫表示が JavaScript で後から描かれる作りだと、この方法では読めない。
  `--probe` で何も出なければそれ。その場合はヘッドレスブラウザが要る。
- `config.json` / `cookies.txt` は認証情報そのもの。`.gitignore` 済みだがコミットしないこと。

## テスト

```bash
python3 -m unittest discover -s tests
```
