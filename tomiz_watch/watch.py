#!/usr/bin/env python3
"""在庫監視 → 復活したら Gmail の下書きを作る。

標準ライブラリのみで動く。ログインが必要なサイトに対応するため、
ブラウザから書き出した Netscape 形式の Cookie ファイルを使う。

使い方:
    python3 watch.py --probe    # ページ中の在庫らしき文言を並べて表示（初回の調整用）
    python3 watch.py --status   # 今の判定結果を表示（状態ファイルも通知も触らない）
    python3 watch.py            # 1回チェックする（launchd から定期実行するのはこれ）
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.message
import email.utils
import gzip
import html
import http.cookiejar
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TOMIZ_WATCH_CONFIG", os.path.join(BASE_DIR, "config.json"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"
LOGIN_REQUIRED = "login_required"
UNKNOWN = "unknown"

log = logging.getLogger("tomiz-watch")


# --------------------------------------------------------------------------
# 設定・状態
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            f"設定ファイルがありません: {CONFIG_PATH}\n"
            f"config.example.json をコピーして作ってください。"
        )
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    for key in ("url", "cookie_file"):
        if not cfg.get(key):
            sys.exit(f"config.json に {key} がありません。")

    cfg.setdefault("label", cfg["url"])
    cfg.setdefault("state_file", os.path.join(BASE_DIR, "state.json"))
    cfg.setdefault("log_file", os.path.join(BASE_DIR, "watch.log"))
    cfg.setdefault("snapshot_dir", os.path.join(BASE_DIR, "snapshots"))
    cfg.setdefault("timeout_seconds", 30)
    cfg.setdefault("retries", 3)
    cfg.setdefault("notify_on_login_required_hours", 12)
    cfg.setdefault("in_stock_markers", ["カートに入れる", "カートへ入れる", "buy-button", "在庫あり"])
    cfg.setdefault(
        "out_of_stock_markers",
        ["在庫切れ", "在庫なし", "売り切れ", "完売", "入荷待ち", "入荷未定", "欠品", "取扱終了", "再入荷"],
    )
    cfg.setdefault("login_markers", ["ログインID", "パスワードを忘れ", "ログインしてください", "/login", "/customer/account/login"])
    cfg.setdefault("expand_cookie_path", True)

    cfg["cookie_file"] = os.path.expanduser(cfg["cookie_file"])
    for key in ("state_file", "log_file", "snapshot_dir"):
        cfg[key] = os.path.expanduser(cfg[key])
    return cfg


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        log.warning("状態ファイルが読めないので初期化します: %s", path)
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------

def fetch(cfg: dict) -> tuple[str, str]:
    """(最終URL, HTML) を返す。"""
    jar = http.cookiejar.MozillaCookieJar()
    if not os.path.exists(cfg["cookie_file"]):
        raise FileNotFoundError(f"Cookie ファイルがありません: {cfg['cookie_file']}")
    # ブラウザ拡張が書き出す Cookie は expires が過去/0 のことがあるので無視して読み込む
    jar.load(cfg["cookie_file"], ignore_discard=True, ignore_expires=True)

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(
        cfg["url"],
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    attempts = cfg.get("retries", 3)
    for attempt in range(1, attempts + 1):
        try:
            return _read(opener, req, cfg)
        except urllib.error.HTTPError as exc:
            # 500番台はサイト側の一時的な不調のことが多い。少し待ってやり直す。
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise
            wait = 5 * 2 ** (attempt - 1)
            log.warning("HTTP %s。%s秒待ってやり直します (%s/%s)", exc.code, wait, attempt, attempts)
            time.sleep(wait)
    raise RuntimeError("到達しない")


def _read(opener, req, cfg: dict) -> tuple[str, str]:
    with opener.open(req, timeout=cfg["timeout_seconds"]) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.geturl(), raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def visible_text(page: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def classify(cfg: dict, final_url: str, page: str) -> tuple[str, list[str]]:
    """(状態, 根拠) を返す。"""
    text = visible_text(page)
    haystack = f"{final_url}\n{page}"

    login_hits = [m for m in cfg["login_markers"] if m in haystack]
    # ログイン画面に飛ばされた場合は URL かフォームで判別する
    if login_hits and ('type="password"' in page or "/login" in final_url):
        return LOGIN_REQUIRED, login_hits

    sentinel = cfg.get("out_of_stock_sentinel")
    if sentinel:
        # 「在庫ありの文言」を当てにいくより、在庫切れの一文が消えたかを見るほうが確実。
        # ただしログイン切れやエラーページでもその一文は消えるので、
        # 商品ページだと確信できる目印を先に確かめる。
        anchor = cfg.get("page_ok_marker")
        if anchor and anchor not in text:
            return UNKNOWN, [f"商品ページの目印が見つからない: {anchor}"]
        if sentinel in text:
            return OUT_OF_STOCK, [sentinel]
        return IN_STOCK, [f"「{sentinel}」が消えた"]

    out_hits = [m for m in cfg["out_of_stock_markers"] if m in text]
    in_hits = [m for m in cfg["in_stock_markers"] if m in text or m in page]

    if out_hits and not in_hits:
        return OUT_OF_STOCK, out_hits
    if in_hits and not out_hits:
        return IN_STOCK, in_hits
    if in_hits and out_hits:
        # 「再入荷のお知らせ」ボタンとカートボタンが同居することがある。
        # 判定できないので unknown にして、人間に見てもらう。
        return UNKNOWN, [f"in={in_hits}", f"out={out_hits}"]
    return UNKNOWN, []


def probe(cfg: dict, page: str) -> None:
    text = visible_text(page)
    keywords = set(cfg["in_stock_markers"]) | set(cfg["out_of_stock_markers"]) | {
        "在庫", "入荷", "カート", "数量", "販売", "お取り寄せ",
    }
    print("--- 在庫に関係しそうな文言 ---")
    found = False
    for kw in sorted(keywords):
        for match in re.finditer(re.escape(kw), text):
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            print(f"[{kw}] …{text[start:end]}…")
            found = True
    if not found:
        print("（見つかりませんでした。ログインできていないか、在庫表示が JavaScript 描画の可能性があります）")
    print(f"\n本文の長さ: {len(text)} 文字")


# --------------------------------------------------------------------------
# 通知（Gmail の下書きを作る）
# --------------------------------------------------------------------------

def build_message(cfg: dict, subject: str, body: str) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["gmail"]["address"]
    msg["To"] = cfg["gmail"].get("to") or cfg["gmail"]["address"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(body)
    return msg


def find_drafts_folder(conn: imaplib.IMAP4_SSL) -> str:
    """\\Drafts 属性を持つフォルダを探す（日本語UIだと [Gmail]/下書き になる）。"""
    typ, data = conn.list()
    if typ == "OK":
        for line in data:
            decoded = line.decode() if isinstance(line, bytes) else line
            if "\\Drafts" in decoded:
                # 例: (\HasNoChildren \Drafts) "/" "[Gmail]/&Tgtm+DBN-"
                parts = decoded.split(' "/" ')
                if len(parts) == 2:
                    return parts[1].strip().strip('"')
    return "[Gmail]/Drafts"


def create_gmail_draft(cfg: dict, subject: str, body: str) -> None:
    gmail = cfg["gmail"]
    password = os.environ.get("TOMIZ_WATCH_GMAIL_APP_PASSWORD") or gmail.get("app_password")
    if not password:
        raise RuntimeError(
            "Gmail のアプリパスワードが設定されていません "
            "(環境変数 TOMIZ_WATCH_GMAIL_APP_PASSWORD か config.json の gmail.app_password)"
        )

    msg = build_message(cfg, subject, body)
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
    try:
        conn.login(gmail["address"], password)
        folder = gmail.get("drafts_folder") or find_drafts_folder(conn)
        typ, resp = conn.append(
            f'"{folder}"', r"\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
        if typ != "OK":
            raise RuntimeError(f"下書きの作成に失敗しました: {typ} {resp}")
        log.info("Gmail に下書きを作成しました（フォルダ: %s / 件名: %s）", folder, subject)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - logout の失敗は本筋ではない
            pass


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

class NotifyFailed(RuntimeError):
    """下書きを作れなかった。状態を進めずに次回やり直す。"""


def notify(cfg: dict, subject: str, body: str) -> None:
    try:
        create_gmail_draft(cfg, subject, body)
    except Exception as exc:  # noqa: BLE001 - 通信もIMAPも何で失敗するか読みきれない
        raise NotifyFailed(str(exc)) from exc


def save_snapshot(cfg: dict, page: str) -> str:
    os.makedirs(cfg["snapshot_dir"], exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(cfg["snapshot_dir"], f"{stamp}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


def setup_logging(cfg: dict, to_stderr: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(cfg["log_file"], encoding="utf-8")]
    if to_stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def run_check(cfg: dict) -> int:
    state = load_state(cfg["state_file"])
    previous = state.get("status", UNKNOWN)

    try:
        final_url, page = fetch(cfg)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.error("取得に失敗しました: %s", exc)
        state["last_error"] = f"{dt.datetime.now().isoformat(timespec='seconds')} {exc}"
        save_state(cfg["state_file"], state)
        return 1

    status, evidence = classify(cfg, final_url, page)
    now = dt.datetime.now()
    log.info("判定: %s (前回: %s) 根拠: %s", status, previous, evidence or "なし")

    state.update(
        {
            "status": status,
            "evidence": evidence,
            "final_url": final_url,
            "checked_at": now.isoformat(timespec="seconds"),
        }
    )
    state.pop("last_error", None)

    if status == LOGIN_REQUIRED:
        # Cookie が切れている。黙って落ちると「在庫切れのまま」と誤解するので知らせる。
        last = state.get("login_notified_at")
        cooldown = dt.timedelta(hours=cfg["notify_on_login_required_hours"])
        if not last or now - dt.datetime.fromisoformat(last) > cooldown:
            notify(
                cfg,
                f"[在庫監視] ログインし直しが必要です: {cfg['label']}",
                "\n".join(
                    [
                        "在庫監視スクリプトがログイン画面に飛ばされました。",
                        "ブラウザで再度ログインして、Cookie を書き出し直してください。",
                        "",
                        f"URL: {cfg['url']}",
                        f"飛ばされた先: {final_url}",
                        f"Cookie ファイル: {cfg['cookie_file']}",
                        f"確認時刻: {now:%Y-%m-%d %H:%M:%S}",
                    ]
                ),
            )
            state["login_notified_at"] = now.isoformat(timespec="seconds")
        save_state(cfg["state_file"], state)
        return 2

    state.pop("login_notified_at", None)

    if status == IN_STOCK and previous != IN_STOCK:
        snapshot = save_snapshot(cfg, page)
        notify(
            cfg,
            f"[在庫復活] {cfg['label']}",
            "\n".join(
                [
                    "在庫が復活したようです。",
                    "",
                    f"商品: {cfg['label']}",
                    f"URL: {cfg['url']}",
                    f"判定根拠: {', '.join(evidence) or '（なし）'}",
                    f"前回の状態: {previous}",
                    f"確認時刻: {now:%Y-%m-%d %H:%M:%S}",
                    "",
                    f"取得したページ: {snapshot}",
                ]
            ),
        )
        state["notified_at"] = now.isoformat(timespec="seconds")

    if status == UNKNOWN and previous != UNKNOWN:
        # 判定できない = 表示が変わった可能性。在庫復活を取りこぼすより知らせる。
        snapshot = save_snapshot(cfg, page)
        notify(
            cfg,
            f"[在庫監視] 判定できませんでした: {cfg['label']}",
            "\n".join(
                [
                    "在庫ありとも在庫切れとも判定できませんでした。",
                    "ページの作りが変わったか、在庫表示が JavaScript 描画かもしれません。",
                    "",
                    f"URL: {cfg['url']}",
                    f"根拠: {', '.join(evidence) or '（該当文言なし）'}",
                    f"確認時刻: {now:%Y-%m-%d %H:%M:%S}",
                    "",
                    f"取得したページ: {snapshot}",
                    "config.json の in_stock_markers / out_of_stock_markers を調整してください。",
                ]
            ),
        )

    save_state(cfg["state_file"], state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="在庫を監視して復活したら Gmail の下書きを作る")
    parser.add_argument("--probe", action="store_true", help="在庫らしき文言を表示して調整に使う")
    parser.add_argument("--status", action="store_true", help="今の判定だけ表示する（状態も通知も変えない）")
    parser.add_argument("--test-draft", action="store_true", help="テスト用の下書きを1通作って終了する")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg, to_stderr=args.probe or args.status or args.test_draft)

    if args.test_draft:
        create_gmail_draft(
            cfg,
            f"[在庫監視] テスト: {cfg['label']}",
            "下書き作成のテストです。これが Gmail の下書きに入っていれば設定は正しいです。",
        )
        print("下書きを作りました。Gmail の下書きフォルダを確認してください。")
        return 0

    if args.probe or args.status:
        final_url, page = fetch(cfg)
        if args.probe:
            probe(cfg, page)
            return 0
        status, evidence = classify(cfg, final_url, page)
        print(f"最終URL: {final_url}")
        print(f"判定: {status}")
        print(f"根拠: {evidence or 'なし'}")
        return 0

    try:
        return run_check(cfg)
    except NotifyFailed as exc:
        # 状態は保存していないので、次回の実行で同じ通知をやり直す
        log.error("下書きを作れませんでした（次回やり直します）: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
