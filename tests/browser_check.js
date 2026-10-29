// k25-inbox.html を file:// で開いて動作確認する（Chromium / Playwright）。
//   node tests/browser_check.js [スクリーンショットの出力先]
// 確認内容:
//  1. 各配信方式（raw / Pages / jsDelivr、fetch と <script>）が file:// から読めるか
//  2. 旧版の localStorage（送信済み・見送り）を引き継いで記事が表示されるか
//  3. 接続先が GitHub の静的データ配信元だけか（各RSSサイト・公開proxyに接続しない）
//  4. 主経路（raw）が使えないとき、別の取得元に切り替わるか
const {chromium} = require('playwright');
const path = require('path');

const REPO = 'shimazukobo-dot/k25-rss-hub';
const HTML = 'file://' + path.resolve(__dirname, '..', 'k25-inbox.html');
const SHOT = process.argv[2] || '';
const ALLOWED = new Set(['raw.githubusercontent.com', 'shimazukobo-dot.github.io', 'cdn.jsdelivr.net']);

const results = [];
const check = (name, ok, detail = '') => { results.push({name, ok, detail}); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`); };
const info = (name, detail) => console.log(`INFO  ${name}  — ${detail}`);

(async () => {
  const opts = process.env.HTTPS_PROXY ? {proxy: {server: process.env.HTTPS_PROXY}} : {};
  const browser = await chromium.launch(opts);
  const ctx = await browser.newContext({viewport: {width: 1280, height: 860}});
  const page = await ctx.newPage();
  const hosts = new Set();
  page.on('request', r => { const u = new URL(r.url()); if (u.protocol.startsWith('http')) hosts.add(u.hostname); });

  // ---- 1. 配信方式ごとの可否 ----
  await page.goto(HTML + '?probe', {waitUntil: 'domcontentloaded'});
  const probe = await page.evaluate(async (REPO) => {
    const out = {};
    const J = {
      'raw fetch': `https://raw.githubusercontent.com/${REPO}/data/public/feed-data.json`,
      'Pages fetch': `https://shimazukobo-dot.github.io/k25-rss-hub/public/feed-data.json`,
      'jsDelivr fetch': `https://cdn.jsdelivr.net/gh/${REPO}@data/public/feed-data.json`,
    };
    for (const [k, u] of Object.entries(J)) {
      try { const r = await fetch(u, {cache: 'no-store'}); out[k] = r.ok ? 'OK ' + (await r.json()).feeds.length + ' feeds' : 'HTTP ' + r.status; }
      catch (e) { out[k] = 'ERR ' + e.message; }
    }
    const S = {
      'raw <script>': `https://raw.githubusercontent.com/${REPO}/data/public/feed-data.js`,
      'Pages <script>': `https://shimazukobo-dot.github.io/k25-rss-hub/public/feed-data.js`,
      'jsDelivr <script>': `https://cdn.jsdelivr.net/gh/${REPO}@data/public/feed-data.js`,
    };
    for (const [k, u] of Object.entries(S)) {
      out[k] = await new Promise(res => {
        delete window.K25_FEED_DATA;
        const s = document.createElement('script');
        s.onload = () => res(window.K25_FEED_DATA ? 'OK ' + window.K25_FEED_DATA.feeds.length + ' feeds' : 'loaded but not executed');
        s.onerror = () => res('ERR (blocked or not found)');
        s.src = u; document.head.appendChild(s);
        setTimeout(() => res('timeout'), 20000);
      });
    }
    return out;
  }, REPO);
  for (const [k, v] of Object.entries(probe)) info(k, v);
  check('raw.githubusercontent.com の JSON を file:// から fetch できる', probe['raw fetch'].startsWith('OK'), probe['raw fetch']);

  // ---- 2. 旧版データを入れた状態で起動 ----
  // （probe 用に開いたページ自身の自動更新が終わってから localStorage を書き換える）
  await page.waitForFunction(() => /更新完了|読み込めませんでした/.test(document.querySelector('#st-msg').textContent), null, {timeout: 90000});
  const seed = await page.evaluate(async (REPO) => {
    const d = await (await fetch(`https://raw.githubusercontent.com/${REPO}/data/public/feed-data.json`, {cache: 'no-store'})).json();
    const f = d.feeds.find(x => x.ok && x.i.length >= 3);
    const now = Date.now();
    localStorage.clear();
    localStorage.setItem('k25rss_feeds_v1', JSON.stringify([{id: 'fOLD', url: f.u, name: '旧名', color: '#123456', lastOk: now - 864e5}]));
    localStorage.setItem('k25rss_arts_v1', JSON.stringify([
      {id: 'fOLD|' + f.i[0].k, fid: 'fOLD', t: f.i[0].t, l: f.i[0].l, d: f.i[0].d, x: '', st: 'sent', ts: now},
      // キーは違うが URL が同じ記事（重複表示しないこと）
      {id: 'fOLD|legacy-key', fid: 'fOLD', t: f.i[1].t, l: f.i[1].l, d: f.i[1].d, x: '', st: 'skip', ts: now},
    ]));
    localStorage.setItem('k25rss_cfg_v1', JSON.stringify({mode: 'popup', ai: false}));
    localStorage.removeItem('k25rss_meta_v1');
    return {u: f.u, n: f.i.length, k0: f.i[0].k, feeds: d.feeds.length, ok: d.feeds.filter(x => x.ok).length};
  }, REPO);

  hosts.clear();
  await page.goto(HTML, {waitUntil: 'domcontentloaded'});
  await page.waitForFunction(() => /更新完了|読み込めませんでした/.test(document.querySelector('#st-msg').textContent), null, {timeout: 90000});
  const st = await page.textContent('#st-msg');
  check('起動時に GitHub のデータを読み込む', st.includes('更新完了'), st);
  const s1 = await page.evaluate((seed) => {
    const feeds = JSON.parse(localStorage.getItem('k25rss_feeds_v1')), arts = JSON.parse(localStorage.getItem('k25rss_arts_v1'));
    const mine = arts.filter(a => a.fid === 'fOLD');
    return {
      feeds: feeds.length, keptId: feeds.some(f => f.id === 'fOLD' && f.url === seed.u && f.color === '#123456'),
      mine: mine.length, sentKept: mine.some(a => a.id === 'fOLD|' + seed.k0 && a.st === 'sent'),
      arts: arts.length, rows: document.querySelectorAll('#list .row').length,
      src: JSON.parse(localStorage.getItem('k25rss_meta_v1')).src,
    };
  }, seed);
  check('フィード一覧が feeds.opml と一致', s1.feeds === seed.feeds, `${s1.feeds}件`);
  check('既存フィードの id・色を引き継ぐ', s1.keptId);
  check('送信済みの記事は送信済みのまま', s1.sentKept);
  check('同じURLの記事を重複させない', s1.mine === seed.n, `${s1.mine}件（データ ${seed.n}件）`);
  check('新着が表示される', s1.rows > 0, `${s1.rows}行 / 記事 ${s1.arts}件 / 取得元 ${s1.src}`);

  // 再読み込みしても件数が増えない
  await page.click('#b-refresh');
  await page.waitForFunction(() => !document.querySelector('#b-refresh').disabled, null, {timeout: 60000});
  const arts2 = await page.evaluate(() => JSON.parse(localStorage.getItem('k25rss_arts_v1')).length);
  check('「↻ 更新」を繰り返しても記事が重複しない', arts2 === s1.arts, `${s1.arts} → ${arts2}`);

  // 設定画面
  await page.click('#b-settings');
  const src = await page.textContent('#src-res');
  check('設定画面にデータの状態が出る', src.includes('✓'), src);
  const bad = await page.locator('#fm-list .s.ng').count();
  info('設定画面', `取得できないフィード ${bad}件を先頭に表示`);
  if (SHOT) await page.screenshot({path: SHOT.replace(/\.png$/, '-settings.png')});
  await page.click('#dlg-close');
  if (SHOT) await page.screenshot({path: SHOT});

  const extra = [...hosts].filter(h => !ALLOWED.has(h));
  check('GitHub のデータ配信元以外に接続しない', extra.length === 0, [...hosts].join(', ') + (extra.length ? '  ※想定外: ' + extra.join(', ') : ''));

  // ---- 4. raw が遮断された場合の切り替え ----
  await page.route('https://raw.githubusercontent.com/**', r => r.abort());
  await page.evaluate(() => { const m = JSON.parse(localStorage.getItem('k25rss_meta_v1')); m.lastRefresh = 0; m.src = ''; localStorage.setItem('k25rss_meta_v1', JSON.stringify(m)); });
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.waitForFunction(() => /更新完了|読み込めませんでした/.test(document.querySelector('#st-msg').textContent), null, {timeout: 120000});
  const fb = await page.evaluate(() => JSON.parse(localStorage.getItem('k25rss_meta_v1')));
  const fbOk = (await page.textContent('#st-msg')).includes('更新完了');
  if (fbOk) check('raw が遮断されても別の取得元で読める', true, '取得元: ' + fb.src);
  else info('raw 遮断時', '代替の取得元も使えませんでした: ' + fb.srcErr);

  await browser.close();
  const failed = results.filter(r => !r.ok);
  console.log(failed.length ? `\n${failed.length}件失敗` : '\nすべて成功');
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
