import time

import pytest

from fastapi.testclient import TestClient

from vizsentry.app import create_app
from vizsentry.settings import SettingsStore


class FakeSources:
    async def close(self):
        pass

    async def inspect(self, config):
        return {'fields': [{'id': 'image', 'name': '图片', 'type': 'attachment'}, {'id': 'log', 'name': '日志', 'type': 'url'}], 'total': 2, 'preview': [], 'warnings': []}

    async def iter_records(self, config):
        yield {'record_id': 'first', 'images': [], 'log_urls': []}
        yield {'record_id': 'second', 'images': [], 'log_urls': []}


def wait_finished(client, identity):
    for _ in range(100):
        job = client.get(f'/api/jobs/{identity}').json()
        if job['status'] not in ('queued', 'importing', 'running'):
            return job
        time.sleep(.05)
    raise AssertionError(job)


def test_api_job_keeps_empty_records_and_masks_credentials(tmp_path):
    app = create_app(tmp_path, FakeSources())
    with TestClient(app) as client:
        assert client.post('/api/jobs', json={}).status_code == 400
        response = client.put('/api/settings', json={'ai': {'api_key': 'secret-not-in-response'}})
        assert 'secret-not-in-response' not in response.text
        assert response.json()['ai']['api_key_configured']
        # Missing model intentionally uses deterministic fallback, no network.
        job = client.post('/api/jobs', json={'source': {'mode': 'api', 'url': 'https://tenant.feishu.cn/base/test', 'image_field': 'image', 'log_field': 'log'}}).json()
        completed = wait_finished(client, job['id'])
        assert completed['total'] == 2 and completed['completed'] == 2
        rows = client.get(f'/api/jobs/{job["id"]}/rows').json()['items']
        assert [row['record_id'] for row in rows] == ['first', 'second']
        assert all(row['image_verdict'] == '待确认' for row in rows)
        assert client.get(f'/api/jobs/{job["id"]}/export').status_code == 200
        assert client.put('/api/settings', json={}, headers={'Origin': 'https://untrusted.example'}).status_code == 403


def test_log_unicode_byte_paging_round_trips(tmp_path):
    app = create_app(tmp_path, FakeSources())
    with TestClient(app) as client:
        job = app.state.db.create_job({})['id']
        text = '日志🚀中文\n' * 100
        path = tmp_path / 'log.txt'
        path.write_text(text, 'utf-8', newline='')
        app.state.db.add_artifact(job, 'log-text', path, 'text', 'log.txt')
        offset, chunks = 0, []
        while offset is not None:
            data = client.get(f'/api/jobs/{job}/logs/log', params={'offset': offset, 'limit': 17}).json()
            chunks.append(data['text'])
            offset = data['next_offset']
        assert ''.join(chunks) == text


def test_settings_blank_secret_preserved(tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'feishu': {'app_secret': 'keep'}, 'log_sites': [{'domain': 'example.test', 'token': 'keep-site'}]})
    store.update({'feishu': {'app_secret': ''}, 'log_sites': [{'domain': 'example.test', 'token': ''}]})
    assert store.read()['feishu']['app_secret'] == 'keep'
    assert store.read()['log_sites'][0]['token'] == 'keep-site'


def test_connector_http_pairing_upload_and_complete_manifest(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        session = client.post('/api/connector/pair', json={'source': {'mode': 'connector', 'scope': 'all'}}).json()
        sid, token = session['session_id'], session['token']
        prefix = f'/api/connector/{sid}'
        auth = {'X-Connector-Token': token}
        status = client.get(prefix)
        assert token not in status.text
        assert client.put(prefix + '/assets/picture', content=b'ignored', headers={'X-Connector-Token': 'bad'}).status_code == 400
        metadata = {'fields': [{'id': 'image', 'name': '图片', 'type': 17}, {'id': 'log', 'name': '日志', 'type': 15}],
                    'total': 2, 'scope': 'all', 'image_field': 'image', 'log_field': 'log'}
        assert client.post(prefix + '/metadata', json=metadata, headers=auth).status_code == 200
        assert client.put(prefix + '/assets/picture', content=b'attachment bytes', headers=auth).status_code == 200
        records = [{'record_id': 'a', 'images': [], 'log_urls': []}, {'record_id': 'b', 'images': [], 'log_urls': []}]
        assert client.post(prefix + '/batch', json={'offset': 0, 'records': records}, headers=auth).status_code == 200
        assert client.post(prefix + '/finish', json={'total': 2}, headers=auth).status_code == 200
        source = {'mode': 'connector', 'session_id': sid, 'scope': 'all', 'image_field': 'image', 'log_field': 'log'}
        preview = client.post('/api/source/inspect', json=source)
        assert preview.status_code == 200 and preview.json()['total'] == 2
        job = client.post('/api/jobs', json={'source': source}).json()
        assert wait_finished(client, job['id'])['total'] == 2
        rows = client.get(f'/api/jobs/{job["id"]}/rows').json()['items']
        assert [row['record_id'] for row in rows] == ['a', 'b']


@pytest.mark.parametrize('count', [10_000, 100_000])
def test_large_manifest_http_paging_and_progress(tmp_path, count):
    app = create_app(tmp_path, FakeSources())
    with TestClient(app) as client:
        db = app.state.db
        identity = db.create_job({})['id']
        for start in range(0, count, 200):
            db.add_records(identity, [{'record_id': f'row-{i}', 'label': f'中文记录 {i}', 'images': [], 'log_urls': []}
                                      for i in range(start, min(start + 200, count))], start)
        db.update_job(identity, total=count, import_complete=1, status='paused')
        started = time.perf_counter()
        status = client.get(f'/api/jobs/{identity}').json()
        response = client.get(f'/api/jobs/{identity}/rows', params={'offset': count - 50, 'limit': 50})
        page = response.json()
        print(f'{count:,} rows: progress + final HTTP page {time.perf_counter() - started:.3f}s, {len(response.content)} bytes')
        assert status['total'] == count and status['pending'] == count
        assert len(page['items']) == 50 and page['total'] == count
        assert page['items'][0]['record_id'] == f'row-{count - 50}'
        assert page['items'][-1]['ordinal'] == count
        assert len(response.content) < 100_000  # bounded response, never the whole manifest
