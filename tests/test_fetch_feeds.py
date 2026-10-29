"""fetch_feeds.py のテスト。ローカルHTTPサーバーに各種フィードを置いて実行する。

  python -m unittest discover -s tests
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = formatdate(time.time() - 3600, usegmt=True)
OLD = formatdate(time.time() - 100 * 86400, usegmt=True)
ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))

RSS2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>RSS2 Feed</title><link>https://ex.com/</link>
<item><title>新しい記事 &amp; テスト</title><link>https://ex.com/a</link><guid>guid-a</guid><pubDate>{NOW}</pubDate>
<description><![CDATA[<p>本文<b>太字</b>です。<script>alert(1)</script></p>]]></description></item>
<item><title>GUIDなし</title><link>https://ex.com/b</link><pubDate>{NOW}</pubDate><content:encoded><![CDATA[<div>encoded本文</div>]]></content:encoded></item>
<item><title>古い記事</title><link>https://ex.com/old</link><guid>old</guid><pubDate>{OLD}</pubDate></item>
</channel></rss>""".encode()

ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title type="html">Atom &lt;em&gt;entry&lt;/em&gt;</title><id>tag:ex.com,2026:1</id>
<link rel="alternate" href="https://ex.com/atom1"/><link rel="replies" href="https://ex.com/c"/>
<updated>{ISO}</updated><summary>Atom summary</summary></entry></feed>""".encode()

RDF = f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://ex.com/"><title>RDF Feed</title><link>https://ex.com/</link></channel>
<item rdf:about="https://ex.com/rdf1"><title>RDF記事</title><link>https://ex.com/rdf1</link><description>RDF本文</description><dc:date>{ISO}</dc:date></item>
</rdf:RDF>""".encode()

SJIS = f"""<?xml version="1.0" encoding="Shift_JIS"?>
<rss version="2.0"><channel><title>シフトJISのブログ</title>
<item><title>日本語タイトル（Shift_JIS）</title><link>https://ex.com/sjis</link><pubDate>{NOW}</pubDate><description>ソフトウェア表示</description></item>
</channel></rss>""".encode("shift_jis")

EUC = f"""<?xml version="1.0" encoding="EUC-JP"?>
<rss version="2.0"><channel><title>EUCのブログ</title>
<item><title>日本語タイトル（EUC-JP）</title><link>https://ex.com/euc</link><pubDate>{NOW}</pubDate></item>
</channel></rss>""".encode("euc_jp")

# 宣言なし・未エスケープの & を含む壊れたXML
BROKEN = f"""<rss version="2.0"><channel><title>Broken & Co</title>
<item><title>A & B</title><link>https://ex.com/broken?a=1&b=2</link><pubDate>{NOW}</pubDate></item>
</channel></rss>""".encode()

HTML_PAGE = b"""<!doctype html><html><head><title>Site</title>
<link rel="alternate" type="application/rss+xml" href="/rss2"></head><body>hi</body></html>"""

ROUTES = {
    "/rss2": ("application/rss+xml; charset=utf-8", RSS2),
    "/atom": ("application/atom+xml", ATOM),
    "/rdf": ("application/rdf+xml", RDF),
    "/sjis": ("text/xml", SJIS),
    "/euc": ("application/xml", EUC),
    "/broken": ("text/xml", BROKEN),
    "/site": ("text/html; charset=utf-8", HTML_PAGE),
    "/plainhtml": ("text/html", b"<html><body>no feed</body></html>"),
}


class H(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        H.hits.append((self.path, self.headers.get("If-None-Match")))
        if self.path == "/404":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/etag" and self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
            return
        ctype, body = ROUTES.get("/rss2" if self.path == "/etag" else self.path, (None, None))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if self.path == "/etag":
            self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(body)


class FetchFeedsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = Path(tempfile.mkdtemp())
        paths = ["/rss2", "/atom", "/rdf", "/sjis", "/euc", "/broken", "/site", "/plainhtml", "/404", "/etag"]
        outlines = "\n".join(f'<outline type="rss" text="T{p}" xmlUrl="{cls.base}{p}"/>' for p in paths)
        # カテゴリ（入れ子）と重複URLも混ぜる
        opml = f"""<?xml version="1.0"?><opml version="2.0"><body>{outlines}
<outline text="Cat"><outline type="rss" text="dup" xmlUrl="{cls.base}/rss2"/></outline></body></opml>"""
        (cls.tmp / "feeds.opml").write_text(opml, "utf-8")
        cls.run_fetch("none", "out1")
        cls.d1 = json.loads((cls.tmp / "out1/public/feed-data.json").read_text("utf-8"))
        cls.f1 = {f["u"].replace(cls.base, ""): f for f in cls.d1["feeds"]}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    @classmethod
    def run_fetch(cls, prev, out):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/fetch_feeds.py"), "--opml", str(cls.tmp / "feeds.opml"),
                            "--prev", str(cls.tmp / prev), "--out", str(cls.tmp / out)],
                           capture_output=True, text=True, env={"NO_PROXY": "*", "PATH": "/usr/bin:/bin"})
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        return r

    def test_opml_dedup(self):
        self.assertEqual(len(self.d1["feeds"]), 10)

    def test_rss2(self):
        f = self.f1["/rss2"]
        self.assertTrue(f["ok"])
        self.assertEqual(f["ft"], "RSS2 Feed")
        a, b = f["i"]  # 古い記事(100日前)は除外
        self.assertEqual(a["k"], "guid-a")
        self.assertEqual(a["t"], "新しい記事 & テスト")
        self.assertEqual(a["x"], "本文太字です。")
        self.assertGreater(a["d"], 0)
        self.assertEqual(b["k"], "https://ex.com/b")  # guid が無ければ link をキーにする
        self.assertEqual(b["x"], "encoded本文")

    def test_atom(self):
        it = self.f1["/atom"]["i"][0]
        self.assertEqual(it["l"], "https://ex.com/atom1")
        self.assertEqual(it["t"], "Atom entry")
        self.assertEqual(it["k"], "tag:ex.com,2026:1")

    def test_rdf(self):
        f = self.f1["/rdf"]
        self.assertTrue(f["ok"])
        it = f["i"][0]
        self.assertEqual((it["t"], it["l"], it["x"]), ("RDF記事", "https://ex.com/rdf1", "RDF本文"))
        self.assertGreater(it["d"], 0)

    def test_japanese_encodings(self):
        self.assertEqual(self.f1["/sjis"]["i"][0]["t"], "日本語タイトル（Shift_JIS）")
        self.assertEqual(self.f1["/sjis"]["i"][0]["x"], "ソフトウェア表示")
        self.assertEqual(self.f1["/sjis"]["ft"], "シフトJISのブログ")
        self.assertEqual(self.f1["/euc"]["i"][0]["t"], "日本語タイトル（EUC-JP）")

    def test_broken_xml(self):
        f = self.f1["/broken"]
        self.assertTrue(f["ok"])
        self.assertEqual(f["i"][0]["t"], "A & B")

    def test_autodiscovery(self):
        f = self.f1["/site"]
        self.assertTrue(f["ok"])
        self.assertEqual(f["i"][0]["k"], "guid-a")

    def test_errors(self):
        self.assertFalse(self.f1["/404"]["ok"])
        self.assertEqual(self.f1["/404"]["e"], "HTTP 404")
        self.assertFalse(self.f1["/plainhtml"]["ok"])
        self.assertIn("RSS/Atomではありません", self.f1["/plainhtml"]["e"])

    def test_jsonp_matches_json(self):
        js = (self.tmp / "out1/public/feed-data.js").read_text("utf-8")
        self.assertIn("window.K25_FEED_DATA=", js)
        self.assertNotIn("</script", js)

    def test_second_run_reuses_previous(self):
        # 2回目: /etag は 304、/404 は前回データなしのまま。304 でも記事は引き継がれる
        H.hits.clear()
        self.run_fetch("out1", "out2")
        d2 = json.loads((self.tmp / "out2/public/feed-data.json").read_text("utf-8"))
        f2 = {f["u"].replace(self.base, ""): f for f in d2["feeds"]}
        self.assertIn(("/etag", '"v1"'), H.hits)
        self.assertTrue(f2["/etag"]["ok"])
        self.assertEqual([i["k"] for i in f2["/etag"]["i"]], [i["k"] for i in self.f1["/etag"]["i"]])
        # 失敗したフィードも前回の記事を保持する
        state = json.loads((self.tmp / "out1/state/fetch-state.json").read_text("utf-8"))
        self.assertEqual(state[self.base + "/site"]["resolved"], self.base + "/rss2")


if __name__ == "__main__":
    unittest.main()
