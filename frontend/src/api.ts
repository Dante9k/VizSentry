export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      const detail = data.detail ?? data.error ?? data.message;
      if (typeof detail === 'string') message = detail;
      else if (detail) message = JSON.stringify(detail);
    } catch { /* Keep the HTTP status when no JSON was returned. */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) });
export function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败，请稍后重试。'; }
export function formatDate(value: string) { const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
export function formatBytes(value?: number) { if (value == null) return ''; return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB`; }
export const activeStatuses = ['queued', 'importing', 'running', 'pending', 'processing', 'pausing'];
export const statusLabels: Record<string, string> = { queued: '排队中', importing: '读取记录中', running: '分析中', pending: '等待分析', processing: '分析中', paused: '已暂停', pausing: '暂停中', completed: '已完成', completed_with_errors: '已完成 · 有失败行', import_failed: '导入失败', done: '已完成', complete: '已完成', cancelled: '已取消', failed: '失败', error: '失败', waiting_login: '等待登录', waiting: '等待连接', uploading: '传输中', ready: '已就绪' };
export function safeUrl(value?: string) { if (!value) return undefined; try { const url = new URL(value, window.location.origin); return ['http:', 'https:'].includes(url.protocol) ? value : undefined; } catch { return undefined; } }
