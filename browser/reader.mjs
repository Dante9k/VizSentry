// Adapted from feishu-bitable-export d00117fd8f744c09055b62d92415f62ed5999157.
// Copyright (c) 2026 workspace-owner. MIT; see ../third_party/feishu-bitable-export/LICENSE.
import { gunzipSync } from "node:zlib";
const INITIAL_RECORD_LIMIT = 200;
const PAGE_SIZE = 1000;
const REQUEST_TIMEOUT_MS = 30_000;
const MAX_REQUEST_ATTEMPTS = 3;

function parseTarget(sourceUrl) {
  let url;
  try { url = new URL(sourceUrl); } catch { throw new Error("请输入完整的飞书多维表格或 Wiki 链接。"); }
  if (url.protocol !== "https:" || !/(^|\.)feishu\.cn$/i.test(url.hostname)) throw new Error("仅支持 HTTPS 的飞书中国站链接（*.feishu.cn）。");
  const route = url.pathname.match(/^\/(base|wiki)\/([^/?#]+)/)?.[1];
  const token = url.pathname.match(/^\/(?:base|wiki)\/([^/?#]+)/)?.[1];
  const tableId = url.searchParams.get("table");
  const viewId = url.searchParams.get("view");
  if (!route || !token || !tableId || !viewId) throw new Error("链接必须包含 /base 或 /wiki、table 与 view；请复制当前显示多维表格的完整地址。");
  return { url, route, baseToken: route === "base" ? token : null, wikiToken: route === "wiki" ? token : null, tableId, viewId };
}

function delay(milliseconds) { return new Promise((resolve) => setTimeout(resolve, milliseconds)); }

async function fetchJsonInPage(page, requestUrl, label) {
  let lastError;
  for (let attempt = 1; attempt <= MAX_REQUEST_ATTEMPTS; attempt += 1) {
    try {
      const result = await page.evaluate(async ({ url, timeoutMs }) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
          const response = await fetch(url, { credentials: "same-origin", signal: controller.signal });
          const text = await response.text();
          let body = null;
          try { body = JSON.parse(text); } catch { /* response bodies are never logged */ }
          return { status: response.status, retryAfter: response.headers.get("retry-after"), body };
        } finally { clearTimeout(timer); }
      }, { url: requestUrl, timeoutMs: REQUEST_TIMEOUT_MS });
      if (![429, 502, 503, 504].includes(result.status) || attempt === MAX_REQUEST_ATTEMPTS) return result;
      const retryAfter = Number(result.retryAfter);
      await delay(Number.isFinite(retryAfter) ? retryAfter * 1_000 : attempt * 750);
    } catch (error) {
      lastError = error;
      if (attempt < MAX_REQUEST_ATTEMPTS) await delay(attempt * 750);
    }
  }
  throw new Error(`${label} 请求未完成：${lastError?.message ?? "网络连接异常"}`);
}

function decodePayload(value, label) {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") throw new Error(`${label} 未返回可解析数据。`);
  try {
    const text = value.startsWith("H4sI") ? gunzipSync(Buffer.from(value, "base64")).toString("utf8") : value;
    return JSON.parse(text);
  } catch { throw new Error(`${label} 的页面数据格式无法解析。`); }
}

function extractData(response, key, label) {
  if (!response || response.status < 200 || response.status >= 300) throw new Error(`${label} 请求失败（HTTP ${response?.status ?? "未知"}）。请确认已在隔离浏览器中拥有查看权限。`);
  if (response.body?.code !== undefined && response.body.code !== 0) throw new Error(`${label} 被飞书拒绝（错误码 ${response.body.code}）。请确认链接、权限和登录状态。`);
  const payload = response.body?.data?.[key];
  if (payload === null || payload === undefined) throw new Error(`${label} 未返回预期数据。当前页面可能未实际显示目标多维表格。`);
  return decodePayload(payload, label);
}

function requestUrl(target, endpoint, parameters) {
  const apiUrl = new URL(`/space/api/v1/bitable/${encodeURIComponent(target.baseToken)}/${endpoint}`, target.url.origin);
  for (const [key, value] of Object.entries(parameters)) apiUrl.searchParams.set(key, String(value));
  return apiUrl.toString();
}

function buildClientvarsUrl(target) {
  return requestUrl(target, "clientvars", { tableID: target.tableId, viewID: target.viewId, recordLimit: INITIAL_RECORD_LIMIT, ondemandLimit: INITIAL_RECORD_LIMIT, needBase: true, viewLazyLoad: true, ondemandVer: 2, openType: 0, noMissCS: true, optimizationFlag: 1, removeFmlExtra: true });
}

function buildRecordsUrl(target, tableRevision, offset) {
  return requestUrl(target, "records", { tableId: target.tableId, viewId: target.viewId, tableRev: tableRevision, depRev: "{}", viewLazyLoad: true, offset, limit: PAGE_SIZE, tableID: target.tableId, viewID: target.viewId, removeFmlExtra: true });
}

async function resolveWikiBase(page, target, networkBases) {
  if (target.baseToken) return target.baseToken;
  const candidates = await page.evaluate(() => {
    const values = [];
    for (const element of document.querySelectorAll("a[href], iframe[src], [data-url], [data-href]")) {
      for (const value of [element.href, element.src, element.dataset?.url, element.dataset?.href]) if (value) values.push(value);
    }
    for (const entry of performance.getEntriesByType("resource")) values.push(entry.name);
    return values;
  });
  const candidateTokens = candidates
    .map((value) => /\/bitable\/([^/?#]+)|\/base\/([^/?#]+)/.exec(value))
    .filter(Boolean)
    .map((match) => match[1] ?? match[2]);
  const tokens = [...new Set([...networkBases, ...candidateTokens])]
    .filter((token) => token && token !== target.wikiToken);
  const valid = [];
  for (const token of tokens) {
    const probeTarget = { ...target, baseToken: token };
    const response = await fetchJsonInPage(page, buildClientvarsUrl(probeTarget), "Wiki 内嵌多维表格识别");
    if (response.status >= 200 && response.status < 300 && response.body?.code === 0 && response.body?.data?.table) valid.push(token);
  }
  if (valid.length === 1) return valid[0];
  if (valid.length > 1) throw new Error("当前 Wiki 页面显示多个可用多维表格，无法自动判断与 table/view 对应的表。请在目标表中选择“打开多维表格”后复制完整链接。");
  throw new Error("当前 Wiki 页面未暴露可验证的多维表格 Base 标识。请确认该链接打开后实际显示目标多维表格，并在隔离浏览器中完成登录后重试。");
}

export function hasActiveFilter(view) {
  const filter = view?.property?.filterInfo;
  if (filter == null) return false;
  const conditions = Array.isArray(filter) ? filter : filter.conditions;
  if (Array.isArray(conditions)) return conditions.length > 0;
  // Unknown filter shapes cannot establish a complete table.
  return typeof filter !== "object" || Object.keys(filter).length > 0;
}

function assertTableStructure(table, target) {
  const view = table?.viewMap?.[target.viewId];
  if (!table?.meta || !view?.property?.fields || !table?.fieldMap) throw new Error("未能在当前授权页面中读取目标表的字段和视图信息。");
  const expectedRecords = hasActiveFilter(view) ? null : Number(table.meta.recordsNum);
  if (expectedRecords !== null && (!Number.isSafeInteger(expectedRecords) || expectedRecords < 0)) throw new Error("目标表未提供可校验的记录总数，已停止导出。");
  if (table.meta.rev === undefined || table.meta.rev === null) throw new Error("目标表未提供当前修订版本，无法安全读取后续记录。");
  return { view, expectedRecords, tableRevision: table.meta.rev };
}

function normalizeRecords(payload) {
  const recordMap = payload?.recordMap;
  if (!recordMap || typeof recordMap !== "object" || Array.isArray(recordMap)) throw new Error("后续记录未返回 recordMap，飞书页面数据格式可能已变化。");
  const nextOffset = [payload.nextOffset, payload.pageInfo?.nextOffset].find((value) => Number.isSafeInteger(Number(value)) && Number(value) >= 0);
  const hasMore = [payload.hasMore, payload.pageInfo?.hasMore].find((value) => typeof value === "boolean");
  return { recordMap, rankMap: payload?.rankInfo?.rankMap ?? {}, pageCount: Object.keys(recordMap).length, hasMore, nextOffset: nextOffset === undefined ? null : Number(nextOffset) };
}

export { parseTarget, fetchJsonInPage, extractData, buildClientvarsUrl, buildRecordsUrl, resolveWikiBase, assertTableStructure, normalizeRecords };
