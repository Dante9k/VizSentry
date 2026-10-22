from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image, ImageFilter

from vizsentry.image_analysis import PROJECT_ROOT, _brisque_module, analyze_image
from vizsentry.log_reader import fetch_log
from vizsentry.model_client import ModelClient, ModelError, _chunks, _offline_summary


def image_file(tmp_path: Path, name="image.png") -> Path:
    path = tmp_path / name
    rng = np.random.default_rng(20261022)
    Image.fromarray(rng.integers(0, 256, (96, 128, 3), dtype=np.uint8)).save(path)
    return path


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))


async def no_sleep(_):
    pass


def client(tmp_path, **overrides):
    return ModelClient({"base_url": "https://model.example/v1", "model": "vision", "api_key": "test-secret", **overrides}, tmp_path)


def test_image_empty_pure_color_and_decode_failure(tmp_path):
    pure = tmp_path / "white.png"
    Image.new("RGB", (1, 1), "white").save(pure)
    data = analyze_image(str(pure))
    assert data["width"] == data["height"] == 1
    assert data["metrics"]["dominant_color_fraction"] == 1
    assert data["metrics"]["brisque_status"] == "not_applicable"
    assert "image_verdict" not in data  # Signals alone never classify business anomalies.
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"corrupt image")
    assert analyze_image(str(bad))["metrics"]["decoded"] is False
    assert analyze_image(str(tmp_path / "missing.png"))["error"]


def test_image_blur_transparency_and_metric_sampling(tmp_path):
    source = image_file(tmp_path)
    blurred = tmp_path / "blurred.png"
    with Image.open(source) as image:
        image.filter(ImageFilter.GaussianBlur(4)).save(blurred)
    sharp_data, blur_data = analyze_image(str(source)), analyze_image(str(blurred))
    assert sharp_data["metrics"]["laplacian_variance"] > blur_data["metrics"]["laplacian_variance"]
    transparent = tmp_path / "transparent.png"
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(transparent)
    data = analyze_image(str(transparent))
    assert data["metrics"]["transparent_fraction"] == 1
    assert data["metrics"]["brightness_mean"] == 255


def test_brisque_is_deterministic_and_uses_verified_local_weights(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    model = PROJECT_ROOT / ".data" / "models" / "brisque_svm_weights.pt"
    if not model.exists():
        pytest.skip("Run scripts/prepare_models.py --data-dir .data before the optional offline-model test")
    monkeypatch.setenv("VIZSENTRY_DATA_DIR", str(PROJECT_ROOT / ".data"))
    path = image_file(tmp_path)
    first = analyze_image(str(path))
    second = analyze_image(str(path))
    assert first["metrics"]["brisque_status"] == "ready", first
    assert first["metrics"]["brisque"] == second["metrics"]["brisque"]
    damaged = tmp_path / "damaged.pt"
    damaged.write_bytes(model.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        _brisque_module().verify_weights(damaged)


async def test_no_model_preserves_images_and_scans_full_log(tmp_path):
    path = image_file(tmp_path)
    log_path = tmp_path / "large.log"
    log_path.write_text("ok\n" * 200_000 + "ERROR LAST_RECORD\n", encoding="utf-8")
    api = ModelClient({}, tmp_path)
    result = await api.analyze([{"id": "a", "path": str(path), "signals": ["图片偏暗"]}, {"id": "b", "error": "下载失败"}],
                               log_paths=[str(log_path)])
    assert result["image_verdict"] == "待确认"
    assert len(result["images"]) == 2
    assert result["model_status"] == "unconfigured"
    assert "LAST_RECORD" in result["log_summary"]


async def test_missing_log_does_not_drop_image_results(tmp_path):
    path = image_file(tmp_path)
    result = await ModelClient({}, tmp_path).analyze(
        [{"id": "image", "path": str(path)}], log_paths=[str(tmp_path / "missing.log")])
    assert len(result["images"]) == 1
    assert result["images"][0]["id"] == "image"
    assert result["model_status"] == "error"
    assert "不存在" in result["error"]


async def test_model_cache_includes_entire_log_image_and_criteria(tmp_path, monkeypatch):
    path = image_file(tmp_path)
    log_path = tmp_path / "input.log"
    log_path.write_text("第一段\n" + "a" * 1200 + "终点", encoding="utf-8")
    api = client(tmp_path, log_chunk_chars=1000)
    calls = []

    async def chat(messages, max_tokens=1500):
        calls.append(messages)
        if isinstance(messages[-1]["content"], list):
            return {"verdict": "正常", "reason": "图片结构完整，日志错误并未反映在图中"}
        data = json.loads(messages[-1]["content"])["untrusted_log_data"]
        return {"summary": "已处理 " + data[-20:]}

    monkeypatch.setattr(api, "_chat", chat)
    images = [{"id": "img-1", "path": str(path), "metrics": {"brightness_mean": 128}}]
    first = await api.analyze(images, criteria="检查渲染", log_paths=[str(log_path)])
    assert first["image_verdict"] == "正常"
    assert first["log_chunks"] == 2
    assert any("终点" in str(call) for call in calls)
    call_count = len(calls)
    assert (await api.analyze(images, criteria="检查渲染", log_paths=[str(log_path)]))["cached"]
    assert len(calls) == call_count
    duplicate_row = [{**images[0], "id": "another-record-image"}]
    cache_hit = await api.analyze(duplicate_row, criteria="检查渲染", log_paths=[str(log_path)])
    assert cache_hit["cached"] is True
    assert cache_hit["images"][0]["id"] == "another-record-image"
    assert len(calls) == call_count
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(" NEW_ERROR")
    assert not (await api.analyze(images, criteria="检查渲染", log_paths=[str(log_path)]))["cached"]
    assert not (await api.analyze(images, criteria="新标准", log_paths=[str(log_path)]))["cached"]
    Image.new("RGB", (128, 96), "red").save(path)
    assert not (await api.analyze(images, criteria="新标准", log_paths=[str(log_path)]))["cached"]


async def test_per_image_verdicts_aggregate_and_errors_remain_pending(tmp_path, monkeypatch):
    api = client(tmp_path)
    calls = 0

    async def verdict(item, summary, criteria):
        nonlocal calls
        calls += 1
        if item["id"] == "2":
            raise ModelError("模型格式错误")
        return {"id": item["id"], "verdict": "异常", "reason": "图像明显截断"}

    monkeypatch.setattr(api, "_image_verdict", verdict)
    result = await api.analyze([{"id": "1"}, {"id": "2"}])
    assert result["image_verdict"] == "异常"
    assert [item["verdict"] for item in result["images"]] == ["异常", "待确认"]
    assert result["model_status"] == "error"
    assert calls == 2


async def test_model_retries_rate_limit_but_rejects_malformed_output(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"正常","reason":"可见内容完整"}'}}]})

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    result = await client(tmp_path)._chat([{"role": "user", "content": "test"}])
    assert result["verdict"] == "正常"
    assert len(requests) == 2
    assert str(requests[0].url) == "https://model.example/v1/chat/completions"


async def test_model_malformed_response_and_timeout(tmp_path, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if request.url.host == "timeout.example":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "no JSON"}}]})

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(ModelError, match="格式"):
        await client(tmp_path)._chat([])
    with pytest.raises(ModelError, match="超时"):
        await client(tmp_path, base_url="https://timeout.example/v1", retries=1)._chat([])
    assert calls == 3


async def test_model_summary_failure_scans_complete_log_fallback(tmp_path, monkeypatch):
    api = client(tmp_path)
    async def fail(*args, **kwargs):
        raise ModelError("模型限流")
    monkeypatch.setattr(api, "_summary_chunk", fail)
    result = await api.analyze([], "abc\n" * 100_000 + "FATAL tail-id")
    assert "tail-id" in result["log_summary"]
    assert result["model_status"] == "error"
    assert result["image_verdict"] == "待确认"


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030", "utf-16"])
async def test_log_original_bytes_and_full_normalized_text(tmp_path, monkeypatch, encoding):
    text = "日志开始\n" + "你好。\n" * 100_000 + "ERROR 最后一行"
    raw = text.encode(encoding)
    mock_http(monkeypatch, lambda request: httpx.Response(200, content=raw, headers={"Content-Type": "text/plain"}))
    result = await fetch_log("https://logs.example/output.log", tmp_path)
    assert result["status"] == "ready"
    assert result["complete"] is True
    assert result["size"] == len(raw)
    assert Path(result["path"]).read_bytes() == raw
    assert Path(result["text_path"]).read_text(encoding="utf-8") == text


async def test_log_json_html_and_login_not_assumed_complete(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path == "/json":
            return httpx.Response(200, json={"hello": "world"})
        if request.url.path == "/login":
            return httpx.Response(200, text='<html><input type="password">请先登录</html>', headers={"Content-Type": "text/html"})
        return httpx.Response(200, text='<html><script>secret()</script><p>ERROR visible</p></html>', headers={"Content-Type": "text/html"})
    mock_http(monkeypatch, handler)
    assert (await fetch_log("https://example.com/json", tmp_path))["status"] == "ready"
    page = await fetch_log("https://example.com/page", tmp_path)
    assert page["status"] == "partial"
    assert "visible" in Path(page["text_path"]).read_text(encoding="utf-8")
    assert "secret" not in Path(page["text_path"]).read_text(encoding="utf-8")
    login = await fetch_log("https://example.com/login", tmp_path)
    assert login["status"] == "needs_login"
    assert login["complete"] is False


async def test_log_browser_auth_and_rules_callback(tmp_path, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(401))
    invocations = []
    async def browser(url, site, target_dir):
        invocations.append((url, site, target_dir))
        return {"status": "login_required", "raw_path": None, "text_path": None, "error": "请扫码"}
    result = await fetch_log("https://example.com/log", tmp_path, browser_fetch=browser)
    assert result["status"] == "needs_login"
    assert len(invocations) == 1
    await fetch_log("https://example.com/log", tmp_path, {"body_selector": "#log"}, browser)
    assert len(invocations) == 2


async def test_authenticated_bytes_normalized_and_all_original_pages_bundled(tmp_path):
    original = tmp_path / "original.log"
    original.write_bytes("登录后的完整日志".encode("gb18030"))
    async def download(url, site, target_dir):
        return {"status": "complete", "raw_path": str(original), "text_path": str(original), "content_type": "text/plain; charset=gb18030"}
    result = await fetch_log("https://example.com/log", tmp_path, {"download_selector": "#download"}, download)
    assert result["status"] == "ready"
    assert Path(result["text_path"]).read_text(encoding="utf-8") == "登录后的完整日志"
    assert Path(result["path"]).read_bytes() == "登录后的完整日志".encode("gb18030")
    page1, page2, body = tmp_path / "p1.html", tmp_path / "p2.html", tmp_path / "body.txt"
    page1.write_text("<p>One</p>", encoding="utf-8")
    page2.write_text("<p>Two</p>", encoding="utf-8")
    body.write_text("One\nTwo", encoding="utf-8")
    async def pages(url, site, target_dir):
        return {"status": "complete", "raw_path": str(page1), "raw_paths": [str(page1), str(page2)],
                "text_path": str(body), "content_type": "text/html"}
    result = await fetch_log("https://example.com/log", tmp_path, {"next_selector": ".next"}, pages)
    assert result["status"] == "ready"
    assert result["name"].endswith(".zip")
    assert "raw_paths" not in result
    with zipfile.ZipFile(result["path"]) as archive:
        assert len(archive.namelist()) == 2
        assert archive.read(archive.namelist()[1]) == page2.read_bytes()


async def test_log_redirect_does_not_leak_site_token(tmp_path, monkeypatch):
    auth = []
    def handler(request):
        auth.append(request.headers.get("Authorization"))
        if request.url.host == "logs.example":
            return httpx.Response(302, headers={"location": "https://other.example/raw"})
        return httpx.Response(200, text="complete\n")
    mock_http(monkeypatch, handler)
    result = await fetch_log("https://logs.example/raw", tmp_path, {"token": "secret"})
    assert result["status"] == "ready"
    assert auth == ["Bearer secret", None]


async def test_log_partial_download_is_retained(tmp_path, monkeypatch):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"START\n" * 20000
            raise httpx.ReadError("connection interrupted")
    mock_http(monkeypatch, lambda request: httpx.Response(200, stream=BrokenStream()))
    result = await fetch_log("https://logs.example/interrupted.log", tmp_path)
    assert result["status"] == "partial"
    assert result["complete"] is False
    assert Path(result["path"]).stat().st_size > 0
    assert Path(result["text_path"]).read_text(encoding="utf-8").startswith("START")


async def test_invalid_log_url_and_declared_size(tmp_path, monkeypatch):
    assert (await fetch_log("file:///etc/passwd", tmp_path))["status"] == "error"
    mock_http(monkeypatch, lambda request: httpx.Response(200, content=b"short", headers={"Content-Length": "100"}))
    result = await fetch_log("https://logs.example/short", tmp_path)
    assert result["status"] == "partial"
    assert result["size"] == 5
