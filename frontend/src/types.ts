export type Mode = 'api' | 'connector' | 'browser';
export type Scope = 'all' | 'view';
export interface Source { mode: Mode; url: string; scope: Scope; image_field: string; log_field: string; session_id?: string; table_id?: string }
export interface Field { id: string; name: string; type?: string | number }
export interface ImageResult { id?: string; name?: string; url?: string; thumbnail_url?: string; verdict?: string; reason?: string; metrics?: Record<string, unknown>; error?: string }
export interface LogResult { id: string; name?: string; url?: string; status?: string; complete?: boolean; size?: number; error?: string }
export interface ResultRow { record_id: string; ordinal: number; status: string; images: ImageResult[]; image_verdict: string; image_reason?: string; log_summary?: string; logs: LogResult[]; error?: string }
export interface Job { id: string; status: string; total: number; completed: number; failed: number; created_at: string; error?: string; source?: Source; name?: string; demo?: boolean; warnings?: string[]; import_complete?: boolean }
export interface Inspection { fields: Field[]; tables?: { id: string; name: string }[]; total: number; preview: { record_id: string; label?: string; images: ImageResult[]; log_urls: string[] }[]; warnings?: string[]; complete?: boolean }
export interface Pairing { session_id: string; token?: string; status: string; count: number; total?: number; warnings?: string[]; config?: Partial<Source> }
export interface Settings { ai: { base_url: string; model: string; api_key?: string; api_key_configured?: boolean; criteria: string }; feishu: { app_id: string; app_secret?: string; app_secret_configured?: boolean }; concurrency: { download: number; ai: number; browser: number }; log_sites: { domain: string; token?: string; header?: string; body_selector?: string; next_selector?: string; download_selector?: string; completion_selector?: string; max_pages?: number; token_configured?: boolean }[] }
