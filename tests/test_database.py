import json
import time
import tracemalloc

import pytest

from vizsentry.database import Database


@pytest.mark.parametrize('count', [10_000, 100_000])
def test_large_manifest_is_paged_stable_and_recoverable(tmp_path, count):
    db = Database(tmp_path / 'scale.sqlite3')
    identity = db.create_job({'mode': 'fixture'})['id']
    tracemalloc.start()
    start = time.perf_counter()
    for offset in range(0, count, 1000):
        batch = [{'record_id': f'rec-{index}', 'label': '中文记录', 'images': [], 'log_urls': []} for index in range(offset, offset + 1000)]
        db.add_records(identity, batch, offset)
    db.update_job(identity, total=count, import_complete=1, status='running')
    end = db.rows(identity, count - 50, 50)
    assert end['total'] == count
    assert [r['ordinal'] for r in end['items']] == list(range(count - 49, count + 1))
    assert db.claim(identity)['record_id'] == 'rec-0'
    db.finish_row(identity, 'rec-0', {'image_verdict': '正常', 'image_reason': 'fixture'})
    assert db.claim(identity)['record_id'] == 'rec-1'
    db.recover()
    assert db.job(identity)['status'] == 'paused'
    assert db.job(identity)['completed'] == 1
    assert db.job(identity)['pending'] == count - 1
    filtered = db.rows(identity, 0, 50, '正常')
    assert filtered['total'] == 1 and filtered['items'][0]['record_id'] == 'rec-0'
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # Input batches stay bounded even for a six-figure manifest.
    assert peak < 15 * 1024 * 1024
    print(json.dumps({'rows': count, 'seconds': round(time.perf_counter() - start, 3), 'python_peak_mb': round(peak / 1024 / 1024, 2)}))


def test_duplicate_record_rolls_back_batch(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    job = db.create_job({})['id']
    with pytest.raises(Exception):
        db.add_records(job, [{'record_id': 'same'}, {'record_id': 'same'}], 0)
    assert db.job(job)['imported'] == 0
    assert db.rows(job)['total'] == 0


def test_retry_preserves_completed_rows_and_source_order(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    job = db.create_job({})['id']
    db.add_records(job, [{'record_id': str(i)} for i in range(3)], 0)
    db.update_job(job, total=3, import_complete=1, status='running')
    db.finish_row(job, '0', {'image_verdict': '异常'})
    db.finish_row(job, '1', {'image_verdict': '待确认'}, 'timeout')
    db.cancel_pending(job)
    db.retry_rows(job)
    assert [row['status'] for row in db.rows(job)['items']] == ['done', 'pending', 'pending']
    assert db.claim(job)['record_id'] == '1'


def test_restart_preserves_cancellation_and_completed_results(tmp_path):
    db = Database(tmp_path / 'cancelled.sqlite3')
    cancelled = db.create_job({})['id']
    db.add_records(cancelled, [{'record_id': str(index)} for index in range(3)], 0)
    db.update_job(cancelled, total=3, import_complete=1, status='running')
    db.finish_row(cancelled, '0', {'image_verdict': '异常', 'image_reason': '已完成的视觉结论'})
    assert db.claim(cancelled)['record_id'] == '1'
    # Simulate closing after the cancellation flag is saved, with one row in
    # flight and another still waiting. No cancelled row may become runnable.
    db.update_job(cancelled, status='cancelled')

    active = db.create_job({})['id']
    db.add_records(active, [{'record_id': 'unfinished'}], 0)
    db.update_job(active, total=1, import_complete=1, status='running')
    db.claim(active)
    db.recover()
    db.recover()  # Repeated startup recovery is idempotent.

    job = db.job(cancelled)
    assert job['status'] == 'cancelled'
    assert job['completed'] == 1 and job['cancelled'] == 2
    assert job['pending'] == job['processing'] == 0
    rows = db.rows(cancelled)['items']
    assert [row['status'] for row in rows] == ['done', 'cancelled', 'cancelled']
    assert rows[0]['image_reason'] == '已完成的视觉结论'
    assert db.job(active)['status'] == 'paused'
    assert db.job(active)['pending'] == 1
    db.retry_rows(cancelled)
    assert [row['status'] for row in db.rows(cancelled)['items']] == ['done', 'pending', 'pending']
