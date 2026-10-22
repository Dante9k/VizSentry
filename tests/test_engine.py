"""Behavioral task lifecycle tests using a fake source and a controlled analyzer."""
from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from vizsentry import analysis
from vizsentry.database import Database
from vizsentry.engine import TaskEngine
from vizsentry.settings import SettingsStore


class FakeSources:
    def __init__(self, count=8, expected=None, fail_after=None, corrupt_first=False):
        self.count = count
        self.expected = count if expected is None else expected
        self.fail_after = fail_after
        self.inspect_calls = 0
        self.import_calls = 0
        self.download_calls = Counter()
        self.corrupt_first = corrupt_first

    async def inspect(self, config):
        self.inspect_calls += 1
        return {"total": self.expected}

    async def iter_records(self, config):
        self.import_calls += 1
        for index in range(self.count):
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("源表快照读取中断")
            record_id = f"record-{index}"
            yield {"record_id": record_id, "label": record_id,
                   "images": [{"token": record_id, "name": record_id + ".png"}], "log_urls": []}

    async def download_image(self, image, destination, config):
        token = image["token"]
        self.download_calls[token] += 1
        if self.corrupt_first and self.download_calls[token] == 1:
            Path(destination).write_bytes(b"incomplete attachment")
        else:
            Image.new("RGB", (8, 8), "green").save(destination, "PNG")

    async def close(self):
        pass


class ControlledAnalysis:
    def __init__(self):
        self.calls: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail_once: set[str] = set()

    async def analyze(self, images, log_text="", criteria="", log_paths=None):
        identity = images[0]["name"].removesuffix(".png")
        self.calls.append(identity)
        await self.gate.wait()
        result = {"image_verdict": "正常", "image_reason": "测试图像已读", "log_summary": "测试摘要",
                  "images": [{"id": image["id"], "verdict": "正常", "reason": "图像已读"} for image in images]}
        if identity in self.fail_once:
            self.fail_once.remove(identity)
            result.update({"image_verdict": "待确认", "error": "模拟模型临时失败"})
        return result


def setup_engine(tmp_path, monkeypatch, sources=None, detector=None):
    sources = sources or FakeSources()
    controller = ControlledAnalysis()
    monkeypatch.setattr(analysis, "ModelClient", lambda *_args, **_kwargs: controller)
    monkeypatch.setattr(analysis, "analyze_image", detector or (lambda path: {
        "width": 8, "height": 8, "metrics": {"decoded": True, "brisque_status": "not_applicable"}, "signals": []}))
    database = Database(tmp_path / "state.sqlite3")
    engine = TaskEngine(database, SettingsStore(tmp_path), tmp_path, sources)
    # pool=None uses the event-loop executor; no subprocess or external model is needed.
    engine.configure()
    return engine, database, sources, controller


def create(engine):
    return engine.create({"mode": "api", "url": "https://test.feishu.cn/base/test",
                          "image_field": "图片", "log_field": "日志", "scope": "all"})["id"]


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def completed(engine, identity):
    await asyncio.wait_for(asyncio.shield(engine.tasks[identity]), timeout=5)


async def test_pause_finishes_inflight_rows_and_resume_preserves_order(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    model.gate.clear()
    try:
        identity = create(engine)
        await until(lambda: len(model.calls) >= 2)
        await engine.control(identity, "pause")
        model.gate.set()
        await until(lambda: db.job(identity)["processing"] == 0)
        paused = db.job(identity)
        assert paused["status"] == "paused"
        assert paused["pending"] > 0
        paused_calls = len(model.calls)
        await asyncio.sleep(0.03)
        assert len(model.calls) == paused_calls
        await engine.control(identity, "resume")
        await completed(engine, identity)
        result = db.job(identity)
        assert result["status"] == "completed"
        assert result["completed"] == result["total"] == 8
        assert Counter(model.calls) == Counter({f"record-{index}": 1 for index in range(8)})
        assert [row["record_id"] for row in db.rows(identity)["items"]] == [f"record-{index}" for index in range(8)]
        assert sources.import_calls == 1
    finally:
        model.gate.set()
        await engine.close()


async def test_cancel_before_scheduled_import_does_not_restart_job(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    try:
        identity = create(engine)
        await engine.control(identity, "cancel")
        await completed(engine, identity)
        assert db.job(identity)["status"] == "cancelled"
        assert db.job(identity)["imported"] == 0
        assert sources.inspect_calls == 0
        assert model.calls == []
    finally:
        await engine.close()


async def test_cancel_retains_all_rows_and_retry_completes_only_unfinished(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    model.gate.clear()
    try:
        identity = create(engine)
        await until(lambda: len(model.calls) >= 2)
        await engine.control(identity, "cancel")
        model.gate.set()
        await completed(engine, identity)
        cancelled = db.job(identity)
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancelled"] > 0
        assert cancelled["completed"] + cancelled["cancelled"] == 8
        assert db.rows(identity)["total"] == 8
        await engine.control(identity, "retry")
        await completed(engine, identity)
        assert db.job(identity)["status"] == "completed"
        assert db.job(identity)["completed"] == 8
        assert Counter(model.calls) == Counter({f"record-{index}": 1 for index in range(8)})
        assert sources.import_calls == 1
    finally:
        model.gate.set()
        await engine.close()


async def test_failed_row_retry_preserves_successful_rows_and_ids(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    model.fail_once.add("record-2")
    try:
        identity = create(engine)
        await completed(engine, identity)
        first = db.job(identity)
        assert first["status"] == "completed_with_errors" and first["failed"] == 1
        first_ids = [row["images"][0]["id"] for row in db.rows(identity)["items"]]
        await engine.control(identity, "retry")
        await completed(engine, identity)
        assert db.job(identity)["status"] == "completed"
        assert Counter(model.calls)["record-2"] == 2
        assert sum(Counter(model.calls).values()) == 9
        assert [row["images"][0]["id"] for row in db.rows(identity)["items"]] == first_ids
        assert sources.import_calls == 1
        assert set(sources.download_calls.values()) == {1}
    finally:
        await engine.close()


@pytest.mark.parametrize("sources,expected_imported", [
    (FakeSources(count=9, expected=10), 9),
    (FakeSources(count=300, fail_after=205), 200),
])
async def test_incomplete_snapshot_never_starts_analysis(tmp_path, monkeypatch, sources, expected_imported):
    engine, db, _, model = setup_engine(tmp_path, monkeypatch, sources)
    try:
        identity = create(engine)
        await completed(engine, identity)
        result = db.job(identity)
        assert result["status"] == "import_failed"
        assert result["import_complete"] is False
        assert result["imported"] == expected_imported
        assert db.rows(identity)["total"] == expected_imported
        assert model.calls == []
        with pytest.raises(ValueError):
            await engine.control(identity, "resume")
    finally:
        await engine.close()


async def test_retry_redownloads_a_corrupt_attachment(tmp_path, monkeypatch):
    real_detector = analysis.analyze_image
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch,
        FakeSources(count=1, corrupt_first=True), detector=real_detector)
    try:
        identity = create(engine)
        await completed(engine, identity)
        assert db.job(identity)["failed"] == 1
        row = db.rows(identity)["items"][0]
        assert str(tmp_path) not in row["error"]
        await engine.control(identity, "retry")
        await completed(engine, identity)
        assert sources.download_calls["record-0"] == 2
        assert db.job(identity)["status"] == "completed"
        assert db.rows(identity)["total"] == 1
    finally:
        await engine.close()


async def test_restart_recovers_inflight_rows_without_reimport(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    model.gate.clear()
    identity = create(engine)
    await until(lambda: len(model.calls) >= 2)
    await engine.close()
    db.recover()
    recovered = db.job(identity)
    assert recovered["status"] == "paused"
    assert recovered["processing"] == 0
    new_engine = TaskEngine(db, SettingsStore(tmp_path), tmp_path, sources)
    new_engine.configure()
    model.gate.set()
    try:
        await new_engine.control(identity, "resume")
        await completed(new_engine, identity)
        assert db.job(identity)["status"] == "completed"
        assert db.job(identity)["completed"] == 8
        assert db.rows(identity)["total"] == 8
        assert sources.import_calls == 1
    finally:
        await new_engine.close()


async def test_concurrency_saved_during_work_applies_when_all_tasks_finish(tmp_path, monkeypatch):
    engine, db, sources, model = setup_engine(tmp_path, monkeypatch)
    initial_gate = asyncio.Event()
    phase = 0
    active_ai = active_downloads = active_browser = 0
    peaks = {0: {"ai": 0, "download": 0, "browser": 0}, 1: {"ai": 0, "download": 0, "browser": 0}}
    original_analyze = model.analyze
    original_download = sources.download_image

    async def counted_analysis(*args, **kwargs):
        nonlocal active_ai
        current_phase = phase
        active_ai += 1
        peaks[current_phase]["ai"] = max(peaks[current_phase]["ai"], active_ai)
        try:
            if current_phase == 0:
                await initial_gate.wait()
            await asyncio.sleep(0.01)
            return await original_analyze(*args, **kwargs)
        finally:
            active_ai -= 1

    async def counted_download(*args, **kwargs):
        nonlocal active_downloads
        current_phase = phase
        active_downloads += 1
        peaks[current_phase]["download"] = max(peaks[current_phase]["download"], active_downloads)
        try:
            await asyncio.sleep(0.01)
            return await original_download(*args, **kwargs)
        finally:
            active_downloads -= 1

    async def counted_browser(*args, **kwargs):
        nonlocal active_browser
        active_browser += 1
        peaks[phase]["browser"] = max(peaks[phase]["browser"], active_browser)
        try:
            await asyncio.sleep(0.01)
            return {"status": "complete"}
        finally:
            active_browser -= 1

    monkeypatch.setattr(model, "analyze", counted_analysis)
    monkeypatch.setattr(sources, "download_image", counted_download)
    monkeypatch.setattr(sources, "browser_log", counted_browser, raising=False)
    try:
        first, second = create(engine), create(engine)
        await until(lambda: active_ai == 2)
        engine.settings.update({"concurrency": {"download": 1, "ai": 1, "browser": 1}})
        initial_gate.set()
        await asyncio.gather(completed(engine, first), completed(engine, second))
        assert db.job(first)["status"] == db.job(second)["status"] == "completed"
        assert peaks[0]["ai"] == 2
        assert peaks[0]["download"] <= 4

        # Completion must apply pending settings before another job is created.
        # Direct browser reads exercise the newly configured semaphore.
        phase = 1
        await asyncio.gather(*(engine.browser_log("https://log.test/", {}, tmp_path) for _ in range(3)))
        assert peaks[1]["browser"] == 1
        # A subsequent job must also use the new limits without saving again.
        third = create(engine)
        await completed(engine, third)
        assert db.job(third)["completed"] == 8
        assert peaks[1] == {"ai": 1, "download": 1, "browser": 1}
    finally:
        initial_gate.set()
        await engine.close()
