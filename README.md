# k25-rss-hub

`k25-inbox.html`（ローカルで開くRSSリーダー）のためのフィード取得基盤です。
RSSの取得はすべて GitHub Actions が行い、会社PCは GitHub 上の静的データを1つ読むだけです。

```
GitHub Actions（20分ごと）                         会社PC（file:// の k25-inbox.html）
  feeds.opml を読む                                   │
  → 205フィードを取得・解析                           │ fetch 1回（ログイン不要）
  → data ブランチに public/feed-data.json を公開 ───→ raw.githubusercontent.com/…/data/public/feed-data.json
```

会社PCから各RSSサイトや公開CORS proxy（AllOrigins / CodeTabs / corsproxy.io / rss2json）には接続しません。

## 使い方（会社PC）

1. このrepositoryの [`k25-inbox.html`](k25-inbox.html) をダウンロードし、今までの `k25-inbox.html` と置き換える
   （同じ場所・同じファイル名で置き換えれば、送信済み・見送り・Instapaper設定はそのまま引き継がれます）
2. Chrome / Edge で開く。開いたとき・開いている間は10分以上経っていれば自動で最新データを読み込みます

## フィードの追加・削除

`feeds.opml` を GitHub 上で編集して保存します（GitHubにログインできる端末から）。
保存すると workflow が自動で走り、数分で `k25-inbox.html` に反映されます。

## データの取得元（上から順に試し、最初に読めたものを使う）

| 取得元 | URL | 備考 |
|---|---|---|
| GitHub raw | `https://raw.githubusercontent.com/shimazukobo-dot/k25-rss-hub/data/public/feed-data.json` | 主経路。`Access-Control-Allow-Origin: *` のため file:// から fetch 可能。キャッシュ最大5分 |
| GitHub Pages | `https://shimazukobo-dot.github.io/k25-rss-hub/public/feed-data.json` | 予備。Settings → Pages で有効化した場合のみ（下記） |
| jsDelivr | `https://cdn.jsdelivr.net/gh/shimazukobo-dot/k25-rss-hub@data/public/feed-data.json` | 予備。更新のたびに workflow がキャッシュを削除 |
| jsDelivr (script) | `…@data/public/feed-data.js` | fetch が使えない環境向けの JSONP 版 |

raw.githubusercontent.com の `.js` は `text/plain` + `nosniff` で配信されるため、`<script>` としては実行できません（JSONP版は jsDelivr / Pages 経由のみ）。

### GitHub Pages を予備経路として有効にする（任意）

raw.githubusercontent.com が会社ネットワークで遮断された場合に備える設定です。

1. repository の **Settings → Pages**
2. **Source: Deploy from a branch**、**Branch: `data` / `(root)`** を選んで Save

## 構成

| パス | 内容 |
|---|---|
| `feeds.opml` | 登録フィード一覧（正） |
| `scripts/fetch_feeds.py` | 取得・解析・出力（Python, feedparser） |
| `.github/workflows/update-feeds.yml` | 20分ごと・手動・`feeds.opml` 変更時に実行 |
| `.github/workflows/verify-inbox.yml` | `k25-inbox.html` を Chromium の file:// で開いて動作確認（手動・HTML変更時） |
| `tests/` | 取得処理のテスト（RSS 2.0 / Atom / RDF / Shift_JIS / EUC-JP / 壊れたXML / 自動検出 / 304）とブラウザ確認 |
| `data` ブランチ | 生成データ。毎回1コミットに置き換え（履歴は残さない） |

### 生成データ（`data` ブランチ）

- `public/feed-data.json` … 本体。`{v, generated, feeds:[{u, n, h, c, ok, e, lo, ft, i:[{k, t, l, d, x}]}]}`
  - `k` は k25-inbox.html の記事キー（guid → link → title）と同じ規則
  - 45日より古い記事は含めない。1フィード最大30件
- `public/feed-data.js` … 同じ内容の JSONP 版（`window.K25_FEED_DATA=…`）
- `public/status.json` … 件数と失敗したフィードの一覧
- `state/fetch-state.json` … ETag / Last-Modified / 自動検出したフィードURL

取得に失敗したフィードは前回の記事を引き継ぎます。一部のフィードの失敗では workflow は失敗扱いにせず、全フィードが失敗した場合だけ赤くなります。

## 手動実行

Actions → **Update feeds** → **Run workflow**

## ローカルでのテスト

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/fetch_feeds.py --opml feeds.opml --prev prev --out out
```
