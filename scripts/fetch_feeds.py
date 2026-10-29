#!/usr/bin/env python3
"""feeds.opml の全フィードを取得し、k25-inbox.html が読む静的データを生成する。

出力（--out 配下）:
  public/feed-data.json  ... 本体（fetch で読む）
  public/feed-data.js    ... 同じ内容の JSONP 版（<script> で読む）
  public/status.json     ... 件数などの要約（監視用）
  state/fetch-state.json ... ETag / Last-Modified / 自動検出したフィードURL

前回の出力（--prev）があれば、取得に失敗したフィードは前回の記事を引き継ぐ。
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests

SCHEMA = 1
WORKERS = 16
TIMEOUT = (10, 25)            # (接続, 読み込み) 秒
MAX_BYTES = 15 * 1024 * 1024
MAX_ITEMS = 30                # 1フィードあたりの記事数上限
KEEP_DAYS = 45                # k25-inbox.html の KEEP と同じ。これより古い記事は出力しない
TEXT_LEN = 420                # k25-inbox.html の mkItem と同じ切り詰め長
UA = "Mozilla/5.0 (compatible; K25RSSHub/1.0; +https://github.com/shimazukobo-dot/k25-rss-hub)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ACCEPT = "application/rss+xml, application/atom+xml, application/rdf+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"


# ---------- OPML ----------
def read_opml(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    feeds, seen = [], set()

    def walk(el, cat):
        for o in el.findall("outline"):
            url = (o.get("xmlUrl") or o.get("xmlurl") or "").strip()
            name = (o.get("title") or o.get("text") or "").strip()
            if url:
                if url not in seen:
                    seen.add(url)
                    feeds.append({"u": url, "n": name or url, "h": (o.get("htmlUrl") or "").strip(), "c": cat})
            else:
                walk(o, name or cat)

    body = root.find("body")
    if body is not None:
        walk(body, "")
    return feeds


# ---------- テキスト整形（k25-inbox.html の plain() 相当） ----------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def plain(s: str) -> str:
    if not s:
        return ""
    if "<" in s or "&" in s:
        p = _Text()
        try:
            p.feed(s)
            p.close()
            s = "".join(p.out)
        except Exception:
            s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def ts_ms(entry) -> int:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(k)
        if t:
            try:
                return calendar.timegm(t) * 1000
            except Exception:
                pass
    return 0


def entry_link(e) -> str:
    link = (e.get("link") or "").strip()
    if link:
        return link
    for l in e.get("links") or []:
        if l.get("href") and l.get("rel", "alternate") == "alternate":
            return l["href"].strip()
    g = (e.get("id") or "").strip()
    return g if g.startswith(("http://", "https://")) else ""


def to_item(e, base: str, now_ms: int) -> dict | None:
    t = plain(e.get("title", "")) or "(無題)"
    link = entry_link(e)
    # k25-inbox.html の既存データと同じ規則でキーを作る（guid → link → title）。
    # 既存の送信済み・見送りと突き合わせるため、guid/link は解決前の生の値を使う
    key = ((e.get("id") or "").strip() or link or t).strip()[:300]
    if link and not link.startswith(("http://", "https://")):
        link = urljoin(base, link)
    if not key:
        return None
    raw = e.get("summary") or ""
    if not raw and e.get("content"):
        raw = e["content"][0].get("value", "")
    x = plain(raw)[:TEXT_LEN]
    if x == t:
        x = ""
    d = ts_ms(e)
    if d > now_ms + 2 * 86400_000:  # 未来日付は信用しない
        d = 0
    return {"k": key, "t": t, "l": link, "d": d, "x": x}


# ---------- 取得 ----------
class _AltLink(HTMLParser):
    TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")

    def __init__(self):
        super().__init__()
        self.found = None

    def handle_starttag(self, tag, attrs):
        if self.found or tag != "link":
            return
        a = dict(attrs)
        if "alternate" in (a.get("rel") or "").lower() and (a.get("type") or "").lower() in self.TYPES and a.get("href"):
            self.found = a["href"]


def discover(body: bytes, base: str) -> str | None:
    p = _AltLink()
    try:
        p.feed(body[:300_000].decode("utf-8", "replace"))
    except Exception:
        return None
    return urljoin(base, p.found) if p.found else None


def http_get(sess: requests.Session, url: str, headers: dict) -> requests.Response:
    last = None
    for attempt, ua in enumerate((UA, UA, BROWSER_UA)):
        try:
            r = sess.get(url, headers={"User-Agent": ua, "Accept": ACCEPT, **headers},
                         timeout=TIMEOUT, stream=True, allow_redirects=True)
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    raise ValueError("too large")
            r._content = body
            if r.status_code in (403, 406, 429) and ua != BROWSER_UA:
                last = r
                continue  # UA で弾くサイト向けにブラウザUAで再試行
            if r.status_code >= 500 and attempt == 0:
                time.sleep(2)
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if attempt >= 1:
                break
            time.sleep(2)
    if isinstance(last, requests.Response):
        return last
    raise last or RuntimeError("fetch failed")


def parse(body: bytes, ctype: str, url: str):
    # content-location を渡すと guid まで相対URLとして書き換えられるため渡さない
    d = feedparser.parse(body, response_headers={"content-type": ctype})
    if d.get("version") or d.entries:
        return d
    return None


def fetch_one(feed: dict, state: dict, now_ms: int) -> tuple[dict, dict]:
    """戻り値: (結果, 新しい状態)。結果 items が None なら前回分を使う。"""
    sess = requests.Session()
    st = dict(state)
    url = st.get("resolved") or feed["u"]
    headers = {}
    if st.get("etag"):
        headers["If-None-Match"] = st["etag"]
    if st.get("modified"):
        headers["If-Modified-Since"] = st["modified"]
    try:
        r = http_get(sess, url, headers)
        if r.status_code == 304:
            return {"ok": True, "items": None, "title": None, "not_modified": True}, st
        if r.status_code >= 400:
            if st.get("resolved"):
                st.pop("resolved", None)  # 自動検出先が消えた場合は次回やり直す
            return {"ok": False, "err": f"HTTP {r.status_code}"}, st
        ctype = r.headers.get("content-type", "")
        d = parse(r.content, ctype, r.url)
        if d is None:
            alt = discover(r.content, r.url)
            if alt and alt != url:
                r = http_get(sess, alt, {})
                if r.status_code >= 400:
                    return {"ok": False, "err": f"HTTP {r.status_code}（自動検出 {alt}）"}, st
                ctype = r.headers.get("content-type", "")
                d = parse(r.content, ctype, r.url)
                if d is not None:
                    st["resolved"] = alt
            if d is None:
                return {"ok": False, "err": "RSS/Atomではありません" + (f"（{ctype.split(';')[0]}）" if ctype else "")}, st
        st["etag"] = r.headers.get("etag") or None
        st["modified"] = r.headers.get("last-modified") or None
        items = []
        for e in d.entries:
            it = to_item(e, r.url, now_ms)
            if it:
                items.append(it)
        title = plain(d.feed.get("title", ""))
        home = d.feed.get("link", "")
        return {"ok": True, "items": items, "title": title, "home": home}, {k: v for k, v in st.items() if v}
    except Exception as e:
        msg = type(e).__name__
        if isinstance(e, requests.Timeout):
            msg = "timeout"
        elif isinstance(e, requests.ConnectionError):
            msg = "接続できません"
        elif str(e):
            msg = f"{msg}: {e}"
        return {"ok": False, "err": msg[:200]}, st


def trim(items: list[dict], now_ms: int) -> list[dict]:
    cutoff = now_ms - KEEP_DAYS * 86400_000
    seen, out = set(), []
    for it in items:
        if it["k"] in seen or (it["d"] and it["d"] < cutoff):
            continue
        seen.add(it["k"])
        out.append(it)
    out.sort(key=lambda x: x["d"] or now_ms, reverse=True)
    return out[:MAX_ITEMS]


# ---------- 出力 ----------
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def js_safe(s: str) -> str:
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opml", default="feeds.opml")
    ap.add_argument("--prev", default="prev", help="前回の出力ディレクトリ（無ければ無視）")
    ap.add_argument("--out", default="out")
    ap.add_argument("--limit", type=int, default=0, help="テスト用: 先頭N件だけ取得")
    a = ap.parse_args()

    feeds = read_opml(Path(a.opml))
    if a.limit:
        feeds = feeds[: a.limit]
    if not feeds:
        print("feeds.opml にフィードがありません", file=sys.stderr)
        return 1
    prev_dir, out_dir = Path(a.prev), Path(a.out)
    prev = load_json(prev_dir / "public" / "feed-data.json", {})
    prev_feeds = {f["u"]: f for f in prev.get("feeds", [])} if prev.get("v") == SCHEMA else {}
    state = load_json(prev_dir / "state" / "fetch-state.json", {})

    now_ms = int(time.time() * 1000)
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(lambda f: fetch_one(f, state.get(f["u"], {}), now_ms), feeds))

    out_feeds, new_state, n_ok, n_items = [], {}, 0, 0
    for f, (res, st) in zip(feeds, results):
        p = prev_feeds.get(f["u"], {})
        items = res.get("items")
        if items is None:  # 304 または失敗 → 前回分を引き継ぐ
            items = p.get("i", [])
            if res.get("not_modified") and not p:
                # 前回データが無いのに 304 は困るので、次回は条件なしで取り直す
                st = {k: v for k, v in st.items() if k == "resolved"}
        rec = {
            "u": f["u"],
            "n": f["n"],
            "h": f["h"] or res.get("home") or p.get("h", ""),
            "c": f["c"],
            "ok": res["ok"],
            "e": "" if res["ok"] else res.get("err", "error"),
            "lo": now_ms if res["ok"] else p.get("lo", 0),
            "i": trim(items, now_ms),
        }
        if res.get("title"):
            rec["ft"] = res["title"]
        elif p.get("ft"):
            rec["ft"] = p["ft"]
        out_feeds.append(rec)
        if st:
            new_state[f["u"]] = st
        n_ok += res["ok"]
        n_items += len(rec["i"])

    data = {"v": SCHEMA, "generated": now_ms, "feeds": out_feeds}
    summary = {
        "v": SCHEMA, "generated": now_ms,
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ms / 1000)),
        "feeds": len(feeds), "ok": n_ok, "failed": len(feeds) - n_ok, "items": n_items,
        "seconds": round(time.time() - t0, 1),
        "failures": [{"u": f["u"], "n": f["n"], "e": f["e"]} for f in out_feeds if not f["ok"]],
    }

    (out_dir / "public").mkdir(parents=True, exist_ok=True)
    (out_dir / "state").mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "public" / "feed-data.json").write_text(body, "utf-8")
    (out_dir / "public" / "feed-data.js").write_text(
        "/* K25 RSS Hub - generated by GitHub Actions */\n"
        f"window.K25_FEED_DATA={js_safe(body)};\n"
        "if(typeof window.k25FeedDataCallback==='function')window.k25FeedDataCallback(window.K25_FEED_DATA);\n",
        "utf-8")
    (out_dir / "public" / "status.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "state" / "fetch-state.json").write_text(json.dumps(new_state, ensure_ascii=False, indent=0), "utf-8")
    (out_dir / ".nojekyll").write_text("", "utf-8")

    print(f"feeds={len(feeds)} ok={n_ok} failed={len(feeds) - n_ok} items={n_items} "
          f"size={len(body.encode()) // 1024}KB time={summary['seconds']}s")
    for x in summary["failures"]:
        print(f"  NG {x['e']:<30} {x['u']}")
    # 一部のフィードの失敗は正常運用の範囲。全滅した場合だけ失敗扱いにして気付けるようにする
    return 0 if n_ok else 2


if __name__ == "__main__":
    sys.exit(main())
