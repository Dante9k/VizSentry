// Local-only NDJSON RPC worker. The Python parent owns all filesystem paths.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createWriteStream } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createInterface } from 'node:readline';
import { createHash, randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { parseTarget, fetchJsonInPage, extractData, buildClientvarsUrl, buildRecordsUrl,
  resolveWikiBase, assertTableStructure, normalizeRecords, hasActiveFilter } from './reader.mjs';

const root = path.resolve(process.argv[2]);
const pages = new Map();
const readers = new Map();
const loginPages = new Map();
let context;
let contextPromise;

function safeHttp(value) {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('仅支持 HTTP(S) 链接。');
  return url;
}
function outputPath(value) {
  const resolved = path.resolve(value);
  const relative = path.relative(root, resolved);
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('输出路径超出本地数据目录。');
  return resolved;
}
async function getContext() {
  if (!context) {
    if (!contextPromise) contextPromise = chromium.launchPersistentContext(path.join(root, 'browser-profile'), {
      channel: 'chromium', headless: process.env.VIZSENTRY_BROWSER_HEADLESS === '1', viewport: { width: 1280, height: 900 }, acceptDownloads: true,
    }).then(value => {
      context = value;
      context.on('close', () => { context = undefined; contextPromise = undefined; pages.clear(); readers.clear(); loginPages.clear(); });
      return value;
    }).catch(error => { contextPromise = undefined; throw error; });
    await contextPromise;
  }
  return context;
}
async function getPage(url, force = false, headers = {}) {
  safeHttp(url);
  const browser = await getContext();
  let page = pages.get(url);
  if (!page || page.isClosed()) {
    page = await browser.newPage(); pages.set(url, page); force = true;
  }
  await page.unroute('**/*');
  if (Object.keys(headers).length) {
    const origin = new URL(url).origin;
    await page.route('**/*', async route => {
      const outgoing = { ...route.request().headers() };
      // Redirected requests can retain custom headers; actively remove them
      // before navigating or fetching resources from a different origin.
      for (const name of Object.keys(headers)) delete outgoing[name.toLowerCase()];
      if (new URL(route.request().url()).origin === origin) {
        Object.assign(outgoing, headers);
        // continue({headers}) also propagates headers through redirects before
        // route handlers run again. Fetch only this hop, then let the browser
        // follow the fulfilled redirect with its original, unmodified headers.
        const response = await route.fetch({ headers: outgoing, maxRedirects: 0 });
        return route.fulfill({ response });
      }
      return route.continue({ headers: outgoing });
    });
  }
  if (force) page.__initialResponse = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  return page;
}
async function tableState(config) {
  const target = parseTarget(config.url);
  if (config.table_id) target.tableId = config.table_id;
  if (config.view_id) target.viewId = config.view_id;
  const page = await getPage(config.url, true);
  target.baseToken = await resolveWikiBase(page, target, new Set());
  const table = extractData(await fetchJsonInPage(page, buildClientvarsUrl(target), '表格元数据'), 'table', '表格元数据');
  const { view, expectedRecords, tableRevision } = assertTableStructure(table, target);
  if ((config.scope || 'all') === 'all' && hasActiveFilter(view)) {
    throw new Error('当前链接视图存在筛选，无法证明读取全部记录。请选择无筛选视图或使用应用凭证/SDK 连接器。');
  }
  const fields = Object.entries(table.fieldMap).map(([id, f]) => ({ id, name: f.name || id, type: f.type }));
  return { page, target, table, fields, expected: expectedRecords, revision: tableRevision };
}
function cell(record, field) {
  const value = record?.fields?.[field] ?? record?.[field];
  return value && typeof value === 'object' && 'value' in value ? value.value : value;
}
export function normalized(recordId, raw, config, fields) {
  const fieldId = (key) => fields.find(f => [f.id, f.name].includes(config[key]))?.id;
  const imageValue = cell(raw, fieldId('image_field'));
  const images = (Array.isArray(imageValue) ? imageValue : imageValue ? [imageValue] : []).filter(v => v != null).map(v => typeof v === 'string'
    ? { name: '图片', url: v } : { name: String(v.name || v.text || '图片'), token: v.file_token || v.token,
      url: v.link || v.url || v.download_url || v.file_url, mime_type: v.mime_type || v.type });
  const value = cell(raw, fieldId('log_field'));
  const urls = (Array.isArray(value) ? value : [value]).flatMap(v => {
    const text = typeof v === 'object' && v ? (v.link || v.url || v.text || '') : String(v || '');
    return text.match(/https?:\/\/[^\s<>"，；]+/g) || [];
  });
  return { record_id: recordId, label: recordId, images, log_urls: [...new Set(urls)] };
}
function appendBatch(reader, batch) {
  const records = [];
  for (const [recordId, raw] of Object.entries(batch.recordMap)) {
    if (reader.seen.has(recordId)) continue; // Private endpoint deliberately overlaps one row.
    reader.seen.add(recordId);
    records.push(normalized(recordId, raw, reader.config, reader.fields));
  }
  if (reader.expected !== null && reader.seen.size > reader.expected) throw new Error('读取记录超过声明总数，表格可能已更新。请重试。');
  return records;
}
async function nextBatch(reader) {
  if (reader.pages++ > 10000 || reader.offsets.has(reader.offset)) throw new Error('分页位置重复或超过安全上限，未完成导入。');
  reader.offsets.add(reader.offset);
  const data = extractData(await fetchJsonInPage(reader.page, buildRecordsUrl(reader.target, reader.revision, reader.offset), '后续记录'), 'records', '后续记录');
  const batch = normalizeRecords(data);
  const records = appendBatch(reader, batch);
  let done = reader.expected !== null && reader.seen.size === reader.expected;
  if (batch.hasMore === false) {
    if (reader.expected !== null && reader.seen.size !== reader.expected) throw new Error(`记录数校验失败：预期 ${reader.expected}，读取 ${reader.seen.size}。`);
    done = true;
  }
  if (reader.expected === null && batch.hasMore === undefined) throw new Error('筛选视图未提供可靠的分页结束标记，请切换无筛选视图或连接器。');
  if (!done && !records.length) throw new Error('后续分页未增加记录，未完成导入。');
  reader.offset = batch.nextOffset !== null && batch.nextOffset > reader.offset ? batch.nextOffset : reader.offset + Math.max(1, batch.pageCount - 1);
  return { reader_id: reader.id, records, done, total: done ? reader.seen.size : reader.expected };
}

async function saveAuthenticated(url, target, extraHeaders = {}, maxBytes = 256 * 1024 * 1024) {
  let current = safeHttp(url);
  const originalOrigin = current.origin;
  for (let redirects = 0; redirects < 10; redirects++) {
    const cookies = await (await getContext()).cookies(current.href);
    const headers = { ...(current.origin === originalOrigin ? extraHeaders : {}) };
    if (cookies.length) headers.Cookie = cookies.map(c => `${c.name}=${c.value}`).join('; ');
    const response = await fetch(current, { headers, redirect: 'manual', signal: AbortSignal.timeout(60000) });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      await response.body?.cancel();
      current = safeHttp(new URL(response.headers.get('location'), current).href); continue;
    }
    if (!response.ok) { await response.body?.cancel(); throw new Error(`下载失败（HTTP ${response.status}）。请登录后重试。`); }
    const contentType = response.headers.get('content-type') || '';
    let size = 0;
    const limited = new TransformStream({ transform(chunk, controller) {
      size += chunk.length;
      if (size > maxBytes) throw new Error('附件超过单文件 256 MB 安全上限。');
      controller.enqueue(chunk);
    } });
    const destination = outputPath(target);
    await fs.mkdir(path.dirname(destination), { recursive: true });
    const temporary = destination + '.part';
    try {
      await pipeline(Readable.fromWeb(response.body.pipeThrough(limited)), createWriteStream(temporary));
      await fs.rename(temporary, destination);
    } catch (error) { await fs.rm(temporary, { force: true }); throw error; }
    return { content_type: contentType, url: current.href, bytes: size };
  }
  throw new Error('下载重定向次数过多。');
}

async function readLog({ url, site = {}, target_dir }) {
  const directory = outputPath(target_dir);
  await fs.mkdir(directory, { recursive: true });
  const headers = { ...(site.headers || {}) };
  if (site.token) {
    const header = site.header || 'Authorization';
    headers[header] = header.toLowerCase() === 'authorization' && !/^\S+\s/.test(site.token) ? `Bearer ${site.token}` : site.token;
  }
  const page = await getPage(url, true, headers);
  const loginUrl = /(?:^|[/.?&=_-])(login|signin|passport)(?:[/.?&=_-]|$)/i.test(page.url());
  const password = await page.locator('input[type="password"]').count();
  if (loginUrl || password || [401, 403].includes(page.__initialResponse?.status())) {
    const origin = new URL(url).origin;
    const existing = loginPages.get(origin);
    if (existing && existing !== page && !existing.isClosed()) {
      await page.close(); pages.delete(url); await existing.bringToFront();
    } else {
      loginPages.set(origin, page); await page.bringToFront();
    }
    return { url, status: 'login_required', text_path: null, raw_path: null, error: '请在浏览器中扫码或登录，完成后重试此行。' };
  }
  const initialType = page.__initialResponse?.headers()['content-type'] || '';
  if (initialType && !/html/i.test(initialType)) {
    const destination = path.join(directory, 'original.log');
    const result = await saveAuthenticated(url, destination, headers);
    return { url, status: 'complete', raw_path: destination, text_path: destination, ...result };
  }
  if (site.download_selector) {
    const link = page.locator(site.download_selector).first();
    const href = await link.getAttribute('href');
    if (!href) throw new Error('原始日志下载选择器未指向有效链接。');
    const destination = path.join(directory, 'original.log');
    const downloadUrl = new URL(href, page.url());
    const result = await saveAuthenticated(downloadUrl.href, destination, downloadUrl.origin === new URL(url).origin ? headers : {});
    if (/text\/html/i.test(result.content_type)) return { url, status: 'partial', raw_path: destination,
      text_path: destination, error: '下载地址返回网页，尚不能确认完整日志。', ...result };
    return { url, status: 'complete', raw_path: destination, text_path: destination, ...result };
  }
  const maxPages = Math.min(1000, Math.max(1, Number(site.max_pages) || 50));
  const textPath = path.join(directory, 'body.txt');
  await fs.writeFile(textPath, '');
  const rawPaths = [];
  if (page.__initialResponse) {
    const originalPath = path.join(directory, 'original-response.bin');
    await fs.writeFile(originalPath, await page.__initialResponse.body());
    rawPaths.push(originalPath);
  }
  const seen = new Set();
  let complete = false;
  let sawNextControl = false;
  let error = '网页正文可能只显示部分日志；请配置原始文件下载或分页/完成标记。';
  try {
   for (let index = 0; index < maxPages; index++) {
    const content = await page.locator(site.body_selector || 'body').innerText({ timeout: 15000 });
    const fingerprint = createHash('sha256').update(content).digest('hex');
    if (seen.has(fingerprint)) { error = '分页内容重复，已停止并保留已读取部分。'; break; }
    seen.add(fingerprint);
    const rawPath = path.join(directory, `page-${String(index + 1).padStart(4, '0')}.html`);
    await fs.writeFile(rawPath, await page.content(), 'utf8');
    rawPaths.push(rawPath);
    await fs.appendFile(textPath, (index ? '\n\n' : '') + content, 'utf8');
    if (site.completion_selector && await page.locator(site.completion_selector).count()) { complete = true; break; }
    if (!site.next_selector) break;
    const next = page.locator(site.next_selector).first();
    if (!await next.count()) {
      complete = sawNextControl;
      if (!complete) error = '第一页找不到配置的下一页控件，无法确认日志完整性。';
      break;
    }
    sawNextControl = true;
    if (await next.isDisabled() || await next.getAttribute('aria-disabled') === 'true') {
      complete = true; break;
    }
    const previous = content;
    await next.click();
    try {
      await page.waitForFunction(({ selector, previous }) => document.querySelector(selector)?.innerText !== previous,
        { selector: site.body_selector || 'body', previous }, { timeout: 15000 });
    } catch { error = '点击下一页后正文没有变化，日志读取不完整。'; break; }
    if (index === maxPages - 1) error = '达到配置的分页上限，日志读取不完整。';
   }
  } catch (failure) {
    error = `网页读取中断，已保留读取内容：${failure.message}`;
  }
  return { url, status: complete ? 'complete' : 'partial', text_path: textPath, raw_path: rawPaths[0],
    raw_paths: rawPaths, content_type: 'text/html', error: complete ? null : error };
}

const methods = {
  async login({ url }) {
    const origin = safeHttp(url).origin;
    let page = loginPages.get(origin);
    if (!page || page.isClosed()) { page = await getPage(url, true); loginPages.set(origin, page); }
    await page.bringToFront(); return { session_id: 'local-browser', status: 'awaiting_login' };
  },
  async inspect({ config }) {
    const state = await tableState(config);
    return { fields: state.fields, tables: [{ id: state.target.tableId, name: state.table.meta.name || state.target.tableId }],
      total: state.expected, table_id: state.target.tableId,
      preview: Object.entries(state.table.recordMap || {}).slice(0, 5).map(([id, r]) => normalized(id, r, config, state.fields)),
      scope: config.scope || 'all', warnings: ['仅扫码接入依赖飞书网页接口；该适配器须用真实表格验收。'] };
  },
  async start({ config }) {
    const state = await tableState(config);
    for (const key of ['image_field', 'log_field']) if (!state.fields.some(f => [f.id, f.name].includes(config[key]))) throw new Error('请选择有效图片列和日志链接列。');
    const initial = normalizeRecords({ recordMap: state.table.recordMap || {}, rankInfo: state.table.rankInfo || {} });
    const id = randomUUID();
    const reader = { ...state, config, id, seen: new Set(), offsets: new Set(), offset: Math.max(0, initial.pageCount - 1), pages: 0 };
    delete reader.table;
    readers.set(id, reader);
    const records = appendBatch(reader, initial);
    return { reader_id: id, records, done: state.expected !== null && records.length === state.expected, total: state.expected };
  },
  async next({ reader_id }) { const reader = readers.get(reader_id); if (!reader) throw new Error('浏览器导入会话已过期，请重试。'); return nextBatch(reader); },
  async release({ reader_id }) { readers.delete(reader_id); return {}; },
  async download({ image, target, config }) {
    await getPage(config.url);
    if (!image.url) throw new Error('飞书网页未提供可下载的附件链接；请使用应用凭证或连接器导入。');
    return saveAuthenticated(image.url, target);
  },
  async log(params) {
    let result;
    try { result = await readLog(params); return result; }
    finally {
      if (result?.status !== 'login_required') {
        const page = pages.get(params.url);
        if (page && !page.isClosed()) await page.close();
        pages.delete(params.url);
        const origin = new URL(params.url).origin;
        if (loginPages.get(origin) === page) loginPages.delete(origin);
      }
    }
  },
};

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Set();
function safeError(error, params = {}) {
  let message = String(error?.message || error);
  const secrets = [params.site?.token, params.config?.app_secret, ...Object.values(params.site?.headers || {})].filter(Boolean);
  for (const secret of secrets) message = message.replaceAll(String(secret), '[redacted]');
  return message.replace(/(authorization|cookie|set-cookie):[^\r\n]*/gi, '$1: [redacted]');
}
async function dispatch(line) {
  let message;
  try {
    message = JSON.parse(line);
    if (!methods[message.method]) throw new Error('不支持的浏览器操作。');
    const result = await methods[message.method](message.params || {});
    if (result?.error) result.error = safeError(result.error, message.params);
    process.stdout.write(JSON.stringify({ id: message.id, result }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: message?.id, error: safeError(error, message?.params) }) + '\n');
  }
}
for await (const line of input) {
  const task = dispatch(line);
  pending.add(task);
  task.finally(() => pending.delete(task));
}
await Promise.allSettled(pending);
await context?.close();
