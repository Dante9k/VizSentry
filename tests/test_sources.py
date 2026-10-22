import asyncio
import json
from pathlib import Path

import httpx
import pytest

from vizsentry.sources import SourceError, SourceService, normalize_record, parse_target


CONFIG = {"mode": "api", "url": "https://demo.feishu.cn/base/base123?table=tbl123&view=vew123",
          "scope": "all", "image_field": "截图", "log_field": "日志", "app_id": "app", "app_secret": "secret"}
FIELDS = [{"id": "img", "name": "截图", "type": 17}, {"id": "log", "name": "日志", "type": 15}]


def test_url_validation_and_scope():
    assert parse_target(CONFIG)["table_id"] == "tbl123"
    for url in ("https://feishu.cn.evil.test/base/a", "file:///tmp/a", "http://demo.feishu.cn/base/a"):
        with pytest.raises(SourceError):
            parse_target({**CONFIG, "url": url})
    with pytest.raises(SourceError):
        parse_target({**CONFIG, "scope": "view", "url": "https://demo.feishu.cn/base/a?table=t"})


def test_normalization_preserves_empty_and_multi_image_rows():
    raw = {"record_id": "r1", "fields": {"截图": [{"file_token": "t", "name": "一.png"}, {"name": "二.png", "url": "https://cdn.test/2"}], "日志": {"link": "https://log.test/1", "text": "日志"}}}
    row = normalize_record(raw, CONFIG, FIELDS)
    assert row["record_id"] == "r1" and len(row["images"]) == 2
    assert row["images"][0]["token"] == "t"
    assert row["log_urls"] == ["https://log.test/1"]
    empty = normalize_record({"record_id": "r2", "fields": {}}, CONFIG, FIELDS)
    assert empty["images"] == [] and empty["log_urls"] == []


def api_handler(record_pages, seen_requests=None):
    def handler(request):
        if seen_requests is not None:
            seen_requests.append(request)
        p = request.url.path
        if "/auth/" in p:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant", "expire": 7200})
        if p.endswith("/tables"):
            data = {"items": [{"table_id": "tbl123", "name": "测试表"}], "has_more": False}
        elif p.endswith("/fields"):
            data = {"items": [{"field_id": f["id"], "field_name": f["name"], "type": f["type"]} for f in FIELDS], "has_more": False}
        elif p.endswith("/records"):
            data = record_pages(request.url.params.get("page_token"))
        else:
            raise AssertionError(p)
        return httpx.Response(200, json={"code": 0, "data": data})
    return handler


def run_records(tmp_path, pages, config=None, seen=None):
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(api_handler(pages, seen)))
        try:
            return [r async for r in service.iter_records(config or CONFIG)]
        finally:
            await service.close()
    return asyncio.run(execute())


def test_api_paginated_rows_and_view_scope(tmp_path):
    requests = []
    pages = lambda token: {"total": 3, "items": [{"record_id": "r3", "fields": {}}], "has_more": False} if token else {"total": 3, "items": [{"record_id": "r1", "fields": {}}, {"record_id": "r2", "fields": {}}], "has_more": True, "page_token": "next"}
    rows = run_records(tmp_path, pages, {**CONFIG, "scope": "view"}, requests)
    assert [r["record_id"] for r in rows] == ["r1", "r2", "r3"]
    assert all(r.url.params["view_id"] == "vew123" for r in requests if r.url.path.endswith("/records"))
    assert len([r for r in requests if "/auth/" in r.url.path]) == 1


@pytest.mark.parametrize("pages", [
    lambda token: {"total": 2, "items": [{"record_id": "r1"}], "has_more": False},
    lambda token: {"total": 2, "items": [{"record_id": "r1"}], "has_more": True},
    lambda token: {"total": 2, "items": [{"record_id": "r1"}], "has_more": bool(not token), "page_token": "next"},
    lambda token: {"total": 2 if not token else 3, "items": [{"record_id": "r1" if not token else "r2"}], "has_more": not token, "page_token": "next"},
])
def test_api_refuses_incomplete_duplicate_or_changing_records(tmp_path, pages):
    with pytest.raises(SourceError):
        run_records(tmp_path, pages)


def test_api_empty_table(tmp_path):
    assert run_records(tmp_path, lambda _: {"total": 0, "items": [], "has_more": False}) == []


def test_api_permission_error_is_clear(tmp_path):
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"code": 99991672})))
        try:
            with pytest.raises(SourceError, match="凭证校验失败"):
                await service.inspect(CONFIG)
        finally:
            await service.close()
    asyncio.run(execute())


def test_api_refreshes_expired_tenant_token(tmp_path, monkeypatch):
    tokens = []
    refused = []
    base = api_handler(lambda _: {"total": 0, "items": [], "has_more": False})
    def handler(request):
        if "/auth/" in request.url.path:
            tokens.append(str(len(tokens)))
            return httpx.Response(200, json={"code": 0, "tenant_access_token": tokens[-1], "expire": 7200})
        if request.url.path.endswith("/records") and not refused:
            refused.append(True)
            return httpx.Response(401, json={"code": 99991663})
        return base(request)
    async def no_sleep(_):
        pass
    monkeypatch.setattr("vizsentry.sources.asyncio.sleep", no_sleep)
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(handler))
        try:
            assert [r async for r in service.iter_records(CONFIG)] == []
            assert len(tokens) == 2
        finally:
            await service.close()
    asyncio.run(execute())


def test_direct_image_does_not_receive_app_credentials(tmp_path):
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(200, content=b"some image")
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(handler))
        try:
            await service.download_image({"url": "https://cdn.test/image.png"}, tmp_path / "image.png", CONFIG)
            assert "authorization" not in observed[0].headers
            assert (tmp_path / "image.png").read_bytes() == b"some image"
        finally:
            await service.close()
    asyncio.run(execute())


@pytest.mark.parametrize("hint", ["url", "tmp_url", "permission"])
def test_advanced_attachment_permissions_and_redirect_boundaries(tmp_path, hint):
    extra = {"bitablePerm": {"tableId": "tbl123", "rev": 32}}
    image = {"file_token": "file123", "name": "image.png"}
    if hint == "url":
        image["url"] = "https://open.feishu.cn/open-apis/drive/v1/medias/file123/download?extra=" + json.dumps(extra)
    elif hint == "tmp_url":
        image["tmp_url"] = "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url?file_tokens=file123&extra=" + json.dumps(extra)
    normalized = normalize_record({"record_id": "r1", "fields": {"截图": [image]}}, CONFIG, FIELDS)["images"][0]
    assert normalized["permission"] == {"tableId": "tbl123", "fieldId": "img", "recordId": "r1"}
    if hint == "tmp_url":
        assert normalized["tmp_url"] == image["tmp_url"]
    seen = []
    def handler(request):
        seen.append(request)
        if "/auth/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant", "expire": 7200})
        if request.url.path.endswith("/download"):
            permission = json.loads(request.url.params["extra"])["bitablePerm"]
            assert permission == (extra["bitablePerm"] if hint != "permission" else {"tableId": "tbl123", "attachments": {"img": {"r1": ["file123"]}}})
            return httpx.Response(302, headers={"Location": "/cdn-hop"})
        if request.url.path == "/cdn-hop":
            assert request.headers["Authorization"] == "Bearer tenant"
            return httpx.Response(302, headers={"Location": "https://cdn.test/asset"})
        assert request.url.host == "cdn.test"
        assert "Authorization" not in request.headers and "extra" not in request.url.params
        return httpx.Response(200, content=b"image")
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(handler))
        try:
            await service.download_image(normalized, tmp_path / "attachment.png", CONFIG)
            assert (tmp_path / "attachment.png").read_bytes() == b"image"
        finally:
            await service.close()
    asyncio.run(execute())


def test_untrusted_attachment_url_never_receives_bearer_or_supplies_extra(tmp_path):
    seen = []
    def handler(request):
        seen.append(request)
        if "/auth/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant", "expire": 7200})
        assert request.url.host == "open.feishu.cn" and "extra" not in request.url.params
        return httpx.Response(200, content=b"image")
    async def execute():
        service = SourceService(tmp_path, transport=httpx.MockTransport(handler))
        try:
            await service.download_image({"token": "file123", "url": "https://evil.test/open-apis/drive/v1/medias/file123/download?extra=untrusted"}, tmp_path / "image.png", CONFIG)
        finally:
            await service.close()
    asyncio.run(execute())
    assert all(request.url.host == "open.feishu.cn" for request in seen)


def test_connector_complete_snapshot_survives_restart(tmp_path):
    service = SourceService(tmp_path)
    session = service.create_connector_session({"url": CONFIG["url"], "scope": "all"})
    sid, token = session["session_id"], session["token"]
    service.connector_metadata(sid, token, {"fields": FIELDS, "total": 2, "table_id": "tbl123"})
    service.write_connector_asset(sid, token, "asset_1", b"image bytes")
    rows = [{"record_id": "a", "images": [{"name": "一.png", "asset_id": "asset_1"}], "log_urls": []},
            {"record_id": "b", "images": [], "log_urls": ["https://log.test/2"]}]
    service.connector_batch(sid, token, 0, rows)
    service.connector_finish(sid, token, 2)
    async def execute():
        await service.close()
        new = SourceService(tmp_path)
        config = {"mode": "connector", "session_id": sid}
        try:
            assert (await new.inspect(config))["total"] == 2
            assert [r["record_id"] async for r in new.iter_records(config)] == ["a", "b"]
            dest = tmp_path / "copy.png"
            await new.download_image(rows[0]["images"][0], dest, config)
            assert dest.read_bytes() == b"image bytes"
        finally:
            await new.close()
    asyncio.run(execute())


def test_connector_refuses_wrong_tokens_wrong_order_duplicates_and_early_finish(tmp_path):
    service = SourceService(tmp_path)
    session = service.create_connector_session({})
    sid, token = session["session_id"], session["token"]
    with pytest.raises(SourceError, match="配对码"):
        service.connector_metadata(sid, "wrong", {"fields": FIELDS, "total": 2})
    service.connector_metadata(sid, token, {"fields": FIELDS, "total": 2})
    row = {"record_id": "r1", "images": [], "log_urls": []}
    with pytest.raises(SourceError):
        service.connector_batch(sid, token, 1, [row])
    with pytest.raises(SourceError):
        service.connector_batch(sid, token, 0, [row, row])
    assert service.connector_status(sid)["count"] == 0
    with pytest.raises(SourceError):
        service.connector_finish(sid, token, 2)
    with pytest.raises(SourceError):
        service.write_connector_asset(sid, token, "../outside", b"bad")
    asyncio.run(service.close())


def test_connector_snapshot_fields_and_scope_cannot_be_reinterpreted(tmp_path):
    service = SourceService(tmp_path)
    session = service.create_connector_session({"scope": "view"})
    sid, token = session["session_id"], session["token"]
    service.connector_metadata(sid, token, {"fields": FIELDS, "total": 0, "scope": "view", "image_field": "img", "log_field": "log"})
    service.connector_finish(sid, token, 0)
    async def execute():
        try:
            base = {"mode": "connector", "session_id": sid}
            assert (await service.inspect({**base, "scope": "view", "image_field": "截图"}))["total"] == 0
            with pytest.raises(SourceError, match="范围已固定"):
                await service.inspect({**base, "scope": "all"})
            with pytest.raises(SourceError, match="图片列已固定"):
                await service.inspect({**base, "image_field": "log"})
        finally:
            await service.close()
    asyncio.run(execute())
