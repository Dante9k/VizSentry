from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import weakref
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageOps

from .database import Database
from .demo import create_demo
from .settings import SettingsStore


def thumbnail(source: str, destination: str):
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((640, 400))
        if image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGB')
        image.save(destination, 'PNG', optimize=True)


def init_analysis_worker(data_dir: str):
    os.environ['VIZSENTRY_DATA_DIR'] = data_dir
    # Small independent CPU jobs should not each occupy every CPU thread.
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    os.environ.setdefault('MKL_NUM_THREADS', '2')


def clean_source(source: dict) -> dict:
    return {key: source[key] for key in ('mode', 'url', 'scope', 'image_field', 'log_field', 'table_id', 'view_id', 'session_id') if key in source}


class TaskEngine:
    def __init__(self, database: Database, settings: SettingsStore, data_dir: Path, sources):
        self.db = database
        self.settings = settings
        self.data_dir = data_dir
        self.sources = sources
        self.tasks: dict[str, asyncio.Task] = {}
        self.signals: dict[str, asyncio.Event] = {}
        self.pool: ProcessPoolExecutor | None = None
        self.download_sem = asyncio.Semaphore(4)
        self.ai_sem = asyncio.Semaphore(2)
        self.browser_sem = asyncio.Semaphore(2)
        self.cache_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.stopping = False

    def configure(self):
        limits = self.settings.read()['concurrency']
        self.download_sem = asyncio.Semaphore(limits['download'])
        self.ai_sem = asyncio.Semaphore(limits['ai'])
        self.browser_sem = asyncio.Semaphore(limits['browser'])

    def start(self):
        self.db.recover()
        self.configure()
        self.pool = ProcessPoolExecutor(max_workers=min(2, os.cpu_count() or 1), initializer=init_analysis_worker, initargs=(str(self.data_dir),))

    async def close(self):
        self.stopping = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        await self.sources.close()
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def source_config(self, source: dict) -> dict:
        config = self.settings.read()
        return {**source, **config['feishu'], 'download_concurrency': config['concurrency']['download']}

    def create(self, source: dict, criteria: str = '', demo: bool = False) -> dict:
        job = self.db.create_job(clean_source(source), criteria, demo)
        self.schedule(job['id'])
        return job

    def schedule(self, identity: str):
        if identity not in self.tasks or self.tasks[identity].done():
            if not any(not task.done() for task in self.tasks.values()):
                self.configure()
            self.signals[identity] = asyncio.Event()
            self.tasks[identity] = asyncio.create_task(self.run(identity), name=f'job-{identity}')
            self.tasks[identity].add_done_callback(self._apply_idle_settings)
        self.signals[identity].set()

    def _apply_idle_settings(self, _task):
        # Never replace semaphores while another job might still hold them.
        if not self.stopping and all(task.done() for task in self.tasks.values()):
            self.configure()

    async def control(self, identity: str, operation: str) -> dict:
        job = self.db.job(identity)
        if operation == 'pause':
            if job['status'] != 'running':
                raise ValueError('只有分析中的任务可以暂停；导入阶段可取消')
            self.db.update_job(identity, status='paused')
        elif operation == 'cancel':
            self.db.update_job(identity, status='cancelled')
            self.db.cancel_pending(identity)
            if identity in self.signals:
                self.signals[identity].set()
        elif operation == 'resume':
            if not job['import_complete'] or job['status'] != 'paused':
                raise ValueError('仅支持继续已完整导入的暂停任务')
            self.db.update_job(identity, status='running', error=None)
            self.schedule(identity)
        elif operation == 'retry':
            if not job['import_complete'] or job['status'] in ('running', 'importing', 'queued'):
                raise ValueError('请等待任务停止且记录清单完整后重试失败行')
            self.db.retry_rows(identity)
            self.schedule(identity)
        else:
            raise ValueError('未知操作')
        return self.db.job(identity)

    async def import_records(self, job: dict):
        identity = job['id']
        if self.db.status(identity) == 'cancelled':
            return False
        source = self.source_config(job['source'])
        self.db.update_job(identity, status='importing', error=None)
        if job['demo']:
            records = await asyncio.to_thread(create_demo, self.data_dir / 'jobs' / identity / 'demo')
            self.db.add_records(identity, records, 0)
            imported = len(records)
        else:
            info = await self.sources.inspect(source)
            if self.db.status(identity) == 'cancelled':
                return False
            expected = info.get('total')
            if expected is not None:
                self.db.update_job(identity, total=expected)
            batch, imported = [], 0
            async for record in self.sources.iter_records(source):
                if self.db.status(identity) == 'cancelled':
                    return False
                if not record.get('record_id'):
                    raise ValueError('源记录缺少稳定标识，无法保证逐行对应')
                batch.append(record)
                if len(batch) == 200:
                    await asyncio.to_thread(self.db.add_records, identity, batch, imported)
                    imported += len(batch)
                    batch = []
            if batch:
                await asyncio.to_thread(self.db.add_records, identity, batch, imported)
                imported += len(batch)
            if expected is not None and imported != expected:
                raise ValueError(f'记录数校验失败：应有 {expected} 行，实际读取 {imported} 行；未开始分析')
        if self.db.status(identity) == 'cancelled':
            return False
        self.db.update_job(identity, total=imported, import_complete=1, status='running')
        return True

    async def run(self, identity: str):
        try:
            job = self.db.job(identity)
            if job['status'] == 'cancelled':
                return
            if not job['import_complete'] and not await self.import_records(job):
                return
            while not self.stopping:
                status = self.db.status(identity)
                if status == 'paused':
                    event = self.signals[identity]
                    event.clear()
                    if self.db.status(identity) == 'paused':
                        await event.wait()
                    continue
                if status != 'running':
                    return
                # A fixed worker pool consumes indexed pending rows; no task per source row.
                await asyncio.gather(*(self.worker(identity) for _ in range(4)))
                job = self.db.job(identity)
                if job['status'] == 'running' and job['pending'] == 0 and job['processing'] == 0:
                    self.db.update_job(identity, status='completed_with_errors' if job['failed'] else 'completed')
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            job = self.db.job(identity)
            if job['status'] != 'cancelled':
                self.db.update_job(identity, status='paused' if job['import_complete'] else 'import_failed', error=str(error))

    async def worker(self, identity: str):
        while not self.stopping and self.db.status(identity) == 'running':
            row = self.db.claim(identity)
            if not row:
                return
            try:
                result, error = await self.process_row(self.db.job(identity, counts=False), row)
                self.db.finish_row(identity, row['record_id'], result, error)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.db.finish_row(identity, row['record_id'], {'image_verdict': '待确认', 'image_reason': '处理失败，保留此行以便重试', 'images': [], 'logs': []}, str(error))

    async def browser_log(self, url: str, site: dict, target_dir: Path):
        async with self.browser_sem:
            return await self.sources.browser_log(url, site, target_dir)

    async def process_row(self, job: dict, row: dict) -> tuple[dict, str | None]:
        from .analysis import ModelClient, analyze_image, fetch_log

        identity, source = job['id'], row['source']
        config = self.source_config(job['source'])
        settings = self.settings.read()
        directory = self.data_dir / 'jobs' / identity
        cache = directory / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        errors, images, logs = [], [], []

        async def read_image(index: int, image: dict):
            cache_key = hashlib.sha256(str(image.get('demo_path') or image.get('token') or image.get('url') or image).encode()).hexdigest()
            key = identity + '-image-' + cache_key
            image_id = hashlib.sha256(f'{row["record_id"]}-image-{index}'.encode()).hexdigest()[:24]
            path, thumb = cache / (cache_key + '.image'), cache / (cache_key + '.thumb.png')
            item = {'id': image_id, 'name': image.get('name', f'图片 {index + 1}'), 'verdict': '待确认', 'reason': '', 'metrics': {}}
            lock = self.cache_locks.setdefault(key, asyncio.Lock())
            try:
                async with lock:
                    if not path.exists():
                        async with self.download_sem:
                            temp = path.with_suffix('.download')
                            if job['demo']:
                                await asyncio.to_thread(shutil.copyfile, image['demo_path'], temp)
                            else:
                                await self.sources.download_image(image, temp, config)
                            temp.replace(path)
                    metrics_path = cache / (cache_key + '.metrics.json')
                    weights = self.data_dir / 'models' / 'brisque_svm_weights.pt'
                    fingerprint = ['metrics-v1', weights.stat().st_mtime_ns if weights.exists() else 0,
                        importlib.util.find_spec('torch') is not None]
                    local = None
                    if metrics_path.exists():
                        try:
                            saved = json.loads(metrics_path.read_text('utf-8'))
                            if saved.get('fingerprint') == fingerprint:
                                local = saved.get('result')
                        except (OSError, ValueError):
                            pass
                    if local is None:
                        local = await asyncio.get_running_loop().run_in_executor(self.pool, analyze_image, str(path))
                        if not local.get('error'):
                            metrics_temp = metrics_path.with_suffix('.tmp')
                            metrics_temp.write_text(json.dumps({'fingerprint': fingerprint, 'result': local}, ensure_ascii=False), 'utf-8')
                            metrics_temp.replace(metrics_path)
                    item['metrics'] = local.get('metrics', {})
                    item['signals'] = local.get('signals', [])
                    if local.get('error'):
                        if local.get('metrics', {}).get('decoded') is False:
                            path.unlink(missing_ok=True)
                            metrics_path.unlink(missing_ok=True)
                        raise ValueError(local['error'])
                    if not thumb.exists():
                        await asyncio.to_thread(thumbnail, str(path), str(thumb))
                self.db.add_artifact(identity, image_id, path, 'image', item['name'])
                self.db.add_artifact(identity, image_id + '-thumb', thumb, 'image', item['name'])
                item.update({'path': str(path), 'url': f'/api/jobs/{identity}/assets/{image_id}',
                    'thumbnail_url': f'/api/jobs/{identity}/assets/{image_id}-thumb'})
            except Exception as error:
                item['error'] = str(error)
                item['reason'] = '图片读取或检测失败：' + str(error)
                errors.append(item['reason'])
            return item

        # Bound each row too: a cell may contain thousands of attachments.
        for start in range(0, len(source.get('images', [])), 4):
            group = source['images'][start:start + 4]
            images.extend(await asyncio.gather(*(read_image(start + i, item) for i, item in enumerate(group))))

        async def read_log(index: int, url: str | None, demo_path: str | None = None):
            log_id = hashlib.sha256(f'{row["record_id"]}-log-{index}'.encode()).hexdigest()[:24]
            domain = urlparse(url or '').hostname or ''
            site = next((s for s in settings['log_sites'] if domain == s.get('domain') or domain.endswith('.' + s.get('domain', '\x00'))), {})
            cache_key = hashlib.sha256(((url or demo_path or '') + json.dumps(site, sort_keys=True)).encode()).hexdigest()
            target = cache / ('log-' + cache_key)
            lock = self.cache_locks.setdefault(identity + '-log-' + cache_key, asyncio.Lock())
            try:
                async with lock:
                    target.mkdir(parents=True, exist_ok=True)
                    if demo_path:
                        result = {'name': Path(demo_path).name, 'path': demo_path, 'text_path': demo_path, 'status': 'ready', 'complete': True, 'size': Path(demo_path).stat().st_size}
                    else:
                        manifest = target / 'ready.json'
                        result = None
                        if manifest.exists():
                            try:
                                saved = json.loads(manifest.read_text('utf-8'))
                                if all(saved.get(key) and Path(saved[key]).is_file() for key in ('path', 'text_path')):
                                    result = saved
                            except (OSError, ValueError):
                                pass
                        if result is None:
                            async with self.download_sem:
                                result = await fetch_log(url, target, site, self.browser_log)
                            if result.get('status') == 'ready' and result.get('complete'):
                                manifest_temp = manifest.with_suffix('.tmp')
                                manifest_temp.write_text(json.dumps(result, ensure_ascii=False, default=str), 'utf-8')
                                manifest_temp.replace(manifest)
                result['id'] = log_id
                result.setdefault('name', f'日志 {index + 1}')
                result['url'] = url
                for name, suffix, kind in (('path', '', 'log'), ('text_path', '-text', 'text')):
                    if result.get(name) and Path(result[name]).is_file():
                        self.db.add_artifact(identity, log_id + suffix, Path(result[name]), kind, result['name'])
                if result.get('status') in ('error', 'needs_login', 'partial') or not result.get('complete'):
                    errors.append(result.get('error') or '日志读取不完整，请检查站点适配或登录状态')
                return result
            except Exception as error:
                errors.append(str(error))
                return {'id': log_id, 'name': f'日志 {index + 1}', 'url': url, 'status': 'error', 'complete': False, 'error': str(error), 'size': 0}

        if source.get('demo_log'):
            logs.append(await read_log(0, None, source['demo_log']))
        else:
            for i, url in enumerate(source.get('log_urls', [])):
                logs.append(await read_log(i, url))
        ai_settings = dict(settings['ai'])
        if job['demo']:
            ai_settings['api_key'] = ''
        model = ModelClient(ai_settings, directory / 'model-cache')
        incomplete = [f"日志 {log.get('name', '')}：{log.get('error') or log.get('status')}，内容不完整，不能视为完整日志。" for log in logs if not log.get('complete')]
        async with self.ai_sem:
            analysis = await model.analyze(images, '\n'.join(incomplete), job['criteria'] or ai_settings.get('criteria', ''),
                log_paths=[str(log['text_path']) for log in logs if log.get('text_path') and log.get('status') in ('ready', 'partial')])
        if analysis.get('error'):
            errors.append(str(analysis['error']))
        per_image = {str(image.get('id')): image for image in analysis.get('images', [])}
        for image in images:
            if image['id'] in per_image and not image.get('error'):
                image.update({key: per_image[image['id']][key] for key in ('verdict', 'reason') if key in per_image[image['id']]})
        verdict = analysis.get('image_verdict', '待确认')
        if not images or any(image.get('error') for image in images):
            verdict = '待确认' if verdict != '异常' else verdict
        result = {'images': images, 'logs': logs, 'image_verdict': verdict,
            'image_reason': analysis.get('image_reason', '缺少可用图片' if not images else '等待确认'),
            'log_summary': analysis.get('log_summary', '没有日志链接' if not logs else ''),
            'analysis': {k: v for k, v in analysis.items() if k not in ('images', 'image_verdict', 'image_reason', 'log_summary')}}
        return result, '；'.join(dict.fromkeys(errors)) if errors else None
