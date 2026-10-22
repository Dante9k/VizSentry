import { bitable, type IAttachmentField } from '@lark-base-open/js-sdk';
import './style.css';

type Field = { id: string; name: string; type: number };
type Pair = { session_id: string; status: string; config: { url?: string; scope?: string; image_field?: string; log_field?: string }; fields: Field[] };
type Image = { name: string; token?: string; asset_id?: string; error?: string; mime_type?: string };
type Row = { record_id: string; label: string; images: Image[]; log_urls: string[] };
const app = document.querySelector<HTMLDivElement>('#app')!;
app.innerHTML = `<div class="brand"><span class="mark">V</span><div>VizSentry<small>飞书多维表格连接器</small></div><span class="tag">只读</span></div>
  <h1>将每一行带入分析</h1><p class="intro">在目标多维表格的侧边栏中打开此页面。配对后按批次读取图片与日志链接，图片立即保存在本机。</p>
  <label>配对码<input id="pair-code" autocomplete="off" placeholder="从 VizSentry 复制配对码"></label>
  <button id="connect">连接当前表格</button><p id="table" class="muted"></p>
  <section id="options" hidden><label>图片列<select id="images"></select></label><label>日志链接列<select id="logs"></select></label>
  <label>读取范围<select id="scope"><option value="all">数据表全部记录</option><option value="view">链接指定视图</option></select></label>
  <button id="send">读取并传输到 VizSentry</button></section>
  <div id="status" role="status">等待配对</div><progress id="progress" value="0" max="1" hidden></progress>
  <p class="foot">连接器仅调用读取接口。不会修改飞书表格。传输完成后，请返回 VizSentry 预览并开始分析。若中断，请创建新的配对重新传输。</p>`;
const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const status = (message: string, error = false) => { const e = element('status'); e.textContent = message; e.classList.toggle('error', error); };
const pairInput = element<HTMLInputElement>('pair-code');
const imageSelect = element<HTMLSelectElement>('images');
const logSelect = element<HTMLSelectElement>('logs');
const scopeSelect = element<HTMLSelectElement>('scope');
let session = '', token = '', pair: Pair | null = null;
let table: Awaited<ReturnType<typeof bitable.base.getActiveTable>> | null = null;
let fields: Field[] = [];
const params = new URLSearchParams(location.hash.slice(1) || location.search);
if (params.get('session_id') && params.get('token')) pairInput.value = `${params.get('session_id')}.${params.get('token')}`;
// Keep the secret out of subsequent referrers and screenshots of the address bar.
if (location.search || location.hash) history.replaceState(null, '', location.pathname);

async function request(path: string, body?: unknown, method = 'POST') {
  const response = await fetch(`/api/connector/${session}${path}`, { method, headers: {
    'X-Connector-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
  }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.message || `本机连接失败 (${response.status})`);
  return data;
}
function options(select: HTMLSelectElement, selected?: string, preferredType?: number) {
  select.replaceChildren(...fields.map(f => new Option(f.name, f.id)));
  select.value = fields.find(f => selected === f.id || selected === f.name)?.id || fields.find(f => f.type === preferredType)?.id || fields[0]?.id || '';
}
async function connect() {
  const button = element<HTMLButtonElement>('connect'); button.disabled = true;
  try {
    [session, token] = pairInput.value.trim().split('.');
    if (!/^[a-f0-9]{32}$/.test(session || '') || !token) throw new Error('请粘贴完整配对码：会话标识.配对密钥');
    status('正在连接本机和飞书…');
    pair = await request('', undefined, 'GET') as Pair;
    if (pair.status !== 'waiting') throw new Error('此配对已开始传输。请返回 VizSentry 创建新配对。');
    table = await Promise.race([bitable.base.getActiveTable(), new Promise<never>((_, reject) => setTimeout(() => reject(new Error('请在飞书多维表格侧边栏中打开连接器，普通浏览器页面无法读取表格。')), 12000))]);
    if (pair.config.url) {
      const expected = new URL(pair.config.url).searchParams.get('table');
      if (expected && expected !== table.id) throw new Error('当前飞书数据表与配对链接不一致，请打开链接对应的数据表。');
    }
    fields = (await table.getFieldMetaList()).map(f => ({ id: f.id, name: f.name, type: f.type }));
    options(imageSelect, pair.config.image_field, 17); options(logSelect, pair.config.log_field, 15);
    scopeSelect.value = pair.config.scope || 'all';
    element('table').textContent = `已连接：${await table.getName()} · ${fields.length} 列`;
    element('options').hidden = false;
    status('已连接。确认两列和范围后开始传输。');
  } catch (error) { status(String((error as Error).message), true); }
  finally { button.disabled = false; }
}
function links(value: unknown): string[] {
  const results = (Array.isArray(value) ? value : [value]).flatMap(piece => {
    const v = piece && typeof piece === 'object' ? (piece as { link?: string; text?: string; url?: string }).link || (piece as { url?: string }).url || (piece as { text?: string }).text || '' : String(piece || '');
    return v.match(/https?:\/\/[^\s<>"，；]+/g) || [];
  });
  return [...new Set(results)];
}
async function uploadImage(url: string, assetId: string) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`飞书附件下载失败 (${response.status})`);
  const blob = await response.blob();
  if (blob.size > 256 * 1024 * 1024) throw new Error('图片超过 256 MB。');
  const result = await fetch(`/api/connector/${session}/assets/${assetId}`, { method: 'PUT', headers: {
    'Content-Type': 'application/octet-stream', 'X-Connector-Token': token,
  }, body: blob });
  if (!result.ok) throw new Error(`图片保存到本机失败 (${result.status})`);
}
async function transfer() {
  const send = element<HTMLButtonElement>('send');
  const connectButton = element<HTMLButtonElement>('connect');
  send.disabled = true; connectButton.disabled = true;
  imageSelect.disabled = true; logSelect.disabled = true; scopeSelect.disabled = true;
  try {
    if (!table || !pair) throw new Error('请先连接当前表格。');
    const imageField = fields.find(f => f.id === imageSelect.value);
    if (imageField?.type !== 17) throw new Error('图片列必须是飞书附件类型。');
    const attachment = await table.getField<IAttachmentField>(imageSelect.value);
    const scope = scopeSelect.value;
    const viewId = scope === 'view' ? (pair.config.url ? new URL(pair.config.url).searchParams.get('view') : null) : undefined;
    if (scope === 'view' && !viewId) throw new Error('链接视图模式需要完整链接中的 view 参数。');
    const requestPage = (pageToken?: number) => table!.getRecordsByPage({ pageSize: 200, pageToken, ...(viewId ? { viewId } : {}) });
    let page = await requestPage();
    const total = page.total;
    if (!Number.isSafeInteger(total) || total < 0) throw new Error('SDK 未提供有效记录总数。');
    await request('/metadata', { fields, total, table_id: table.id, view_id: viewId, scope, image_field: imageSelect.value, log_field: logSelect.value });
    const progress = element<HTMLProgressElement>('progress'); progress.hidden = false; progress.max = total || 1;
    let count = 0;
    const seenTokens = new Set<number>();
    while (true) {
      if (page.total !== total) throw new Error('传输期间记录总数发生变化，请新建配对重试。');
      const batch: Row[] = [];
      for (const record of page.records) {
        const rawImages = record.fields[imageSelect.value];
        const items = Array.isArray(rawImages) ? rawImages.filter(item => item && typeof item === 'object') as Array<{ name?: string; token?: string; type?: string }> : [];
        const images: Image[] = [];
        let urls: string[] = [], imageError = '';
        if (items.length) try { urls = await attachment.getAttachmentUrls(record.recordId); }
        catch (error) { imageError = (error as Error).message; }
        for (const [index, item] of items.entries()) {
          const image: Image = { name: item.name || '图片', token: item.token, mime_type: item.type };
          try {
            if (!urls[index]) throw new Error(imageError || '附件未提供下载链接。');
            const assetId = crypto.randomUUID();
            await uploadImage(urls[index], assetId);
            image.asset_id = assetId;
          } catch (error) { image.error = `${(error as Error).message} 请在飞书连接器中创建新配对重新导入此附件。`; }
          images.push(image);
        }
        batch.push({ record_id: record.recordId, label: record.recordId, images, log_urls: links(record.fields[logSelect.value]) });
        status(`正在读取 ${count + batch.length} / ${total} 行…`);
      }
      await request('/batch', { offset: count, records: batch });
      count += batch.length; progress.value = count;
      if (!page.hasMore) break;
      if (page.pageToken === undefined || seenTokens.has(page.pageToken)) throw new Error('SDK 分页标记缺失或重复，未完成传输。');
      seenTokens.add(page.pageToken);
      page = await requestPage(page.pageToken);
    }
    if (count !== total) throw new Error(`记录数校验失败：预期 ${total}，实际 ${count}。`);
    await request('/finish', { total });
    status(`已完整传输 ${total} 行。请返回 VizSentry 预览并开始分析。`);
    send.textContent = '传输完成';
  } catch (error) {
    status(`${(error as Error).message} 如已开始上传，请创建新配对再试。`, true);
    connectButton.disabled = false;
  }
}
element('connect').addEventListener('click', connect);
element('send').addEventListener('click', transfer);
