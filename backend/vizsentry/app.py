from __future__ import annotations

import asyncio
import codecs
import importlib.util
import json
import mimetypes
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import Database
from .engine import TaskEngine, clean_source
from .exporter import export_workbook
from .settings import SettingsStore, default_data_dir

ROOT = Path(__file__).resolve().parents[2]


def create_app(data_dir: Path | None = None, source_service=None) -> FastAPI:
    directory = (data_dir or default_data_dir()).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database = Database(directory / 'vizsentry.sqlite3')
    settings = SettingsStore(directory)
    if source_service is None:
        from .sources import SourceService
        source_service = SourceService(directory)
    engine = TaskEngine(database, settings, directory, source_service)
    export_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine.start()
        yield
        await engine.close()

    app = FastAPI(title='VizSentry', version='0.1.0', lifespan=lifespan)
    app.state.db, app.state.engine, app.state.settings = database, engine, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            parsed = urlparse(origin)
            if parsed.hostname not in ('localhost', '127.0.0.1', '::1', 'testserver'):
                return JSONResponse({'detail': '仅允许本机应用发起操作'}, 403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({'detail': '任务、记录或文件不存在'}, 404)

    @app.exception_handler(ValueError)
    async def bad_input(request, error):
        return JSONResponse({'detail': str(error)}, 400)

    from .sources import SourceError

    @app.exception_handler(SourceError)
    async def source_error(request, error):
        return JSONResponse({'detail': str(error)}, 400)

    def safe_artifact(job_id: str, asset_id: str):
        artifact = database.artifact(job_id, asset_id)
        path = Path(artifact['path']).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise HTTPException(404, '文件不存在')
        return artifact, path

    @app.get('/api/health')
    async def health():
        return {'status': 'ok', 'version': '0.1.0'}

    @app.get('/api/settings')
    async def get_settings():
        return settings.read(masked=True)

    @app.put('/api/settings')
    async def put_settings(body: dict):
        result = settings.update(body)
        if not any(not task.done() for task in engine.tasks.values()):
            engine.configure()
        return result

    @app.post('/api/settings/test-ai')
    async def test_ai():
        from .analysis import ModelClient
        model = ModelClient(settings.read()['ai'], directory / 'model-test')
        try:
            return await model.test()
        except Exception as error:
            raise HTTPException(400, str(error)) from error

    @app.get('/api/diagnostics')
    async def diagnostics():
        config = settings.read()
        from .analysis import inspect_brisque
        model_check = inspect_brisque(directory)
        torch_ready = model_check['torch_installed']
        weights_ready = model_check['weights_valid']
        checks = [
            {'name': '本地服务', 'status': 'ok', 'message': 'FastAPI 与 SQLite 已就绪'},
            {'name': '离线基础检测', 'status': 'ok', 'message': '亮度、纯色、模糊和解码检查可用'},
            {'name': 'BRISQUE', 'status': 'ok' if torch_ready and weights_ready else 'warning', 'message': '模型资源已准备' if torch_ready and weights_ready else '运行 setup.ps1 安装 CPU 模型与评分资源'},
            {'name': 'AI 接口', 'status': 'ok' if config['ai'].get('api_key') and config['ai'].get('model') else 'warning', 'message': '已配置，使用连接测试验证' if config['ai'].get('api_key') else '尚未配置 API Key；仍可运行离线检测'},
            {'name': '飞书应用', 'status': 'ok' if config['feishu'].get('app_id') and config['feishu'].get('app_secret') else 'warning', 'message': '已配置' if config['feishu'].get('app_secret') else '可在设置中配置，或选择网页登录模式'},
            {'name': '网页登录', 'status': 'ok' if (ROOT / 'browser' / 'node_modules' / 'playwright').exists() else 'warning', 'message': '需在真实飞书与日志站点验证登录、权限及分页'},
        ]
        return {'checks': checks, 'data_dir': str(directory), 'live_feishu_verified': False,
            'torch_ready': torch_ready, 'weights_ready': weights_ready, 'notes': ['暂无真实样表，三种飞书连接需在你的环境验收']}

    @app.post('/api/source/inspect')
    async def inspect_source(body: dict):
        config = engine.source_config(clean_source(body))
        try:
            return await source_service.inspect(config)
        except Exception as error:
            raise HTTPException(400, str(error)) from error

    @app.post('/api/browser/login')
    async def browser_login(body: dict):
        url = body.get('url', '')
        if urlparse(url).scheme not in ('http', 'https'):
            raise HTTPException(400, '请输入 HTTP 或 HTTPS 登录地址')
        try:
            return await source_service.login(url)
        except Exception as error:
            raise HTTPException(400, str(error)) from error

    @app.post('/api/jobs')
    async def create_job(body: dict):
        source = clean_source(body.get('source', {}))
        if source.get('mode') not in ('api', 'browser', 'connector'):
            raise HTTPException(400, '请选择飞书接入方式')
        if not source.get('image_field') or not source.get('log_field'):
            raise HTTPException(400, '请选择图片列和日志列')
        source.setdefault('scope', 'all')
        return engine.create(source, str(body.get('criteria', '')))

    @app.post('/api/jobs/demo')
    async def demo():
        return engine.create({'mode': 'demo', 'url': '', 'scope': 'all'}, demo=True)

    @app.get('/api/jobs')
    async def jobs():
        return {'items': database.list_jobs()}

    @app.get('/api/jobs/{job_id}')
    async def job(job_id: str):
        return database.job(job_id)

    @app.get('/api/jobs/{job_id}/rows')
    async def rows(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), verdict: str = ''):
        database.job(job_id)
        result = database.rows(job_id, offset, limit, verdict)
        for row in result['items']:
            for image in row['images']:
                image.pop('path', None)
            for log in row['logs']:
                log.pop('path', None)
                log.pop('text_path', None)
                log['download_url'] = f'/api/jobs/{job_id}/logs/{log["id"]}/download'
        return result

    @app.post('/api/jobs/{job_id}/{operation}')
    async def control(job_id: str, operation: str):
        return await engine.control(job_id, operation)

    @app.get('/api/jobs/{job_id}/assets/{asset_id}')
    async def image_asset(job_id: str, asset_id: str):
        artifact, path = safe_artifact(job_id, asset_id)
        mime = 'image/png' if asset_id.endswith('-thumb') else mimetypes.guess_type(artifact['name'])[0] or 'application/octet-stream'
        return FileResponse(path, media_type=mime)

    @app.get('/api/jobs/{job_id}/logs/{log_id}')
    async def full_log(job_id: str, log_id: str, offset: int = Query(0, ge=0), limit: int = Query(65536, ge=4, le=262144)):
        _, path = safe_artifact(job_id, log_id + '-text')
        total = path.stat().st_size
        with path.open('rb') as file:
            file.seek(offset)
            raw = file.read(limit)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        text = decoder.decode(raw, final=offset + len(raw) >= total)
        pending_bytes = len(decoder.getstate()[0])
        next_offset = offset + len(raw) - pending_bytes
        return {'text': text, 'next_offset': next_offset if next_offset < total else None, 'total': total, 'offset': offset}

    @app.get('/api/jobs/{job_id}/logs/{log_id}/download')
    async def download_log(job_id: str, log_id: str):
        artifact, path = safe_artifact(job_id, log_id)
        return FileResponse(path, filename=Path(artifact['name']).name, media_type='application/octet-stream')

    @app.get('/api/jobs/{job_id}/export')
    async def export(job_id: str):
        async with export_locks.setdefault(job_id, asyncio.Lock()):
            destination = directory / 'jobs' / job_id / 'exports' / 'VizSentry.xlsx'
            await asyncio.to_thread(export_workbook, database, job_id, destination)
        return FileResponse(destination, filename=f'VizSentry-{job_id[:8]}.xlsx', media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def connector_token(request: Request, body: dict | None = None) -> str:
        return request.headers.get('X-Connector-Token') or (body or {}).get('token') or request.query_params.get('token') or ''

    @app.post('/api/connector/pair')
    async def pair(body: dict):
        return source_service.create_connector_session(clean_source(body.get('source', body)))

    @app.get('/api/connector/{session_id}')
    async def connector_status(session_id: str, request: Request):
        # The local UI and same-origin connector both access this endpoint; tokens are never returned.
        if token := connector_token(request):
            source_service.authorize_connector(session_id, token)
        return source_service.connector_status(session_id)

    @app.post('/api/connector/{session_id}/metadata')
    async def connector_metadata(session_id: str, request: Request, body: dict):
        return source_service.connector_metadata(session_id, connector_token(request, body), body)

    @app.post('/api/connector/{session_id}/batch')
    async def connector_batch(session_id: str, request: Request, body: dict):
        return source_service.connector_batch(session_id, connector_token(request, body), int(body.get('offset', 0)), body.get('records', []))

    @app.post('/api/connector/{session_id}/finish')
    async def connector_finish(session_id: str, request: Request, body: dict):
        return source_service.connector_finish(session_id, connector_token(request, body), int(body['total']))

    @app.put('/api/connector/{session_id}/assets/{asset_id}')
    async def connector_asset(session_id: str, asset_id: str, request: Request):
        source_service.authorize_connector(session_id, connector_token(request))
        if not asset_id or len(asset_id) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in asset_id):
            raise HTTPException(400, '附件标识不合法')
        max_size = 256 * 1024 * 1024
        if int(request.headers.get('content-length', '0')) > max_size:
            raise HTTPException(413, '单个附件超过 256 MB')
        uploads = directory / 'uploads'
        uploads.mkdir(exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=uploads)
        try:
            size = 0
            with os.fdopen(descriptor, 'wb') as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_size:
                        raise HTTPException(413, '单个附件超过 256 MB')
                    file.write(chunk)
            return source_service.write_connector_asset(session_id, connector_token(request), asset_id, file_path=Path(temporary))
        finally:
            Path(temporary).unlink(missing_ok=True)

    @app.get('/api/{unmatched:path}')
    async def unknown_api(unmatched: str):
        raise HTTPException(404, '接口不存在')

    if (ROOT / 'connector' / 'dist').exists():
        app.mount('/connector', StaticFiles(directory=ROOT / 'connector' / 'dist', html=True), name='connector')
    if (ROOT / 'frontend' / 'dist').exists():
        app.mount('/', StaticFiles(directory=ROOT / 'frontend' / 'dist', html=True), name='frontend')
    else:
        @app.get('/')
        async def build_needed():
            return JSONResponse({'message': '后端已启动。请运行 setup.ps1 构建网页，或启动 frontend 开发服务器。'})
    return app
