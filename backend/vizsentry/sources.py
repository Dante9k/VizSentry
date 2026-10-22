"""Read-only Feishu adapters and durable, capability-token sidebar imports."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import weakref
from typing import AsyncIterator
from urllib.parse import parse_qs, quote, urlparse

import httpx


class SourceError(RuntimeError):
    pass


def parse_target(config: dict) -> dict:
    url = urlparse(config.get("url", ""))
    if url.scheme != "https" or not url.hostname or not any(
        url.hostname == h or url.hostname.endswith("." + h) for h in ("feishu.cn", "larksuite.com")
    ):
        raise SourceError("请输入 HTTPS 飞书多维表格链接。")
    match = re.match(r"^/(base|wiki)/([A-Za-z0-9]+)", url.path)
    if not match:
        raise SourceError("链接必须包含 /base/ 或 /wiki/。")
    query = parse_qs(url.query)
    table = config.get("table_id") or query.get("table", [None])[0]
    view = config.get("view_id") or query.get("view", [None])[0]
    if config.get("scope", "all") == "view" and not view:
        raise SourceError("仅链接视图模式需要包含 view 参数的完整链接。")
    return {"route": match[1], "token": match[2], "table_id": table, "view_id": view,
            "api": "https://open.larksuite.com/open-apis" if url.hostname.endswith("larksuite.com") else "https://open.feishu.cn/open-apis"}


def _cell(value):
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def extract_urls(value) -> list[str]:
    value = _cell(value)
    result = []
    for piece in value if isinstance(value, list) else [value]:
        if isinstance(piece, dict):
            candidate = piece.get("link") or piece.get("url") or piece.get("text") or ""
        else:
            candidate = str(piece or "")
        for url in re.findall(r"https?://[^\s<>\"，；]+", str(candidate)):
            if url not in result:
                result.append(url)
    return result


def normalize_record(record: dict, config: dict, fields: list[dict]) -> dict:
    values = record.get("fields", record)
    record_id = str(record.get("record_id") or record.get("recordId") or record.get("id") or "")
    if not record_id:
        raise SourceError("来源记录缺少标识，无法保证行对应关系。")
    image_field = next((f for f in fields if config.get("image_field") in (f["id"], f["name"])), {})
    table_id = config.get("table_id") or parse_qs(urlparse(config.get("url", "")).query).get("table", [None])[0]
    def selected(key):
        requested = config.get(key)
        field = next((f for f in fields if requested in (f["id"], f["name"])), None)
        if not field:
            return None
        return _cell(values.get(field["id"], values.get(field["name"])))
    images = []
    image_values = selected("image_field")
    for image in image_values if isinstance(image_values, list) else ([image_values] if image_values else []):
        if isinstance(image, str):
            images.append({"name": "图片", "url": image})
        elif isinstance(image, dict):
            permission = dict(image.get("permission") or {})
            for key, value in (("tableId", table_id), ("recordId", record_id), ("fieldId", image_field.get("id"))):
                if value:
                    permission.setdefault(key, value)
            images.append({"name": str(image.get("name") or image.get("text") or "图片"),
                           "token": image.get("file_token") or image.get("token"),
                           "url": image.get("url") or image.get("link") or image.get("download_url") or image.get("file_url"),
                           "tmp_url": image.get("tmp_url"), "permission": permission,
                           "mime_type": image.get("type") or image.get("mime_type"),
                           "size": image.get("size")})
    return {"record_id": record_id, "label": record_id, "images": images,
            "log_urls": extract_urls(selected("log_field"))}


class BrowserWorker:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.pending = {}
        self.reader_task = None

    async def _read_responses(self, process):
        try:
            while line := await process.stdout.readline():
                response = json.loads(line)
                future = self.pending.pop(response.get("id"), None)
                if future is not None and not future.done():
                    if response.get("error"):
                        future.set_exception(SourceError(response["error"]))
                    else:
                        future.set_result(response.get("result"))
        except (ValueError, asyncio.CancelledError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(SourceError("浏览器组件已退出，请安装 browser 依赖及 Chromium 后重试。"))
            self.pending.clear()

    async def call(self, method: str, **params):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                worker = Path(__file__).resolve().parents[2] / "browser" / "worker.mjs"
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        os.environ.get("VIZSENTRY_NODE", "node"), str(worker), str(self.data_dir),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
                except OSError as exc:
                    raise SourceError("浏览器组件无法启动，请运行安装脚本安装 Node.js 和 Playwright。") from exc
                self.reader_task = asyncio.create_task(self._read_responses(self.process))
            self.sequence += 1
            message = {"id": self.sequence, "method": method, "params": params}
            future = asyncio.get_running_loop().create_future()
            self.pending[self.sequence] = future
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=180)
        except asyncio.TimeoutError as exc:
            raise SourceError("浏览器读取超时，请重试。") from exc
        finally:
            self.pending.pop(message["id"], None)

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task:
            await self.reader_task
            self.reader_task = None
        self.process = None


class SourceService:
    def __init__(self, data_dir: Path, *, transport=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20), transport=transport,
                                        follow_redirects=True)
        self.browser = BrowserWorker(self.data_dir)
        self.tokens = {}
        self.page_locks = weakref.WeakValueDictionary()
        self.connector_dir = self.data_dir / "connector"
        self.connector_dir.mkdir(exist_ok=True)

    async def close(self):
        await self.client.aclose()
        await self.browser.close()

    async def _token(self, config, refresh=False):
        key = (config.get("app_id"), hashlib.sha256(config.get("app_secret", "").encode()).hexdigest())
        if not key[0] or not config.get("app_secret"):
            raise SourceError("请在设置中配置飞书 App ID 和 App Secret。")
        cached = self.tokens.get(key)
        if cached and cached[1] > time.monotonic() and not refresh:
            return cached[0]
        base = parse_target(config)["api"]
        response = await self.client.post(base + "/auth/v3/tenant_access_token/internal",
                                          json={"app_id": key[0], "app_secret": config["app_secret"]})
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise SourceError(f"飞书凭证校验失败（错误码 {data.get('code', '未知')}）。")
        self.tokens[key] = (data["tenant_access_token"], time.monotonic() + max(0, data.get("expire", 7200) - 90))
        return data["tenant_access_token"]

    async def _api(self, config, path, params=None):
        base = parse_target(config)["api"]
        refresh = False
        for attempt in range(4):
            token = await self._token(config, refresh=attempt > 0 and refresh)
            response = await self.client.get(base + path, params=params, headers={"Authorization": f"Bearer {token}"})
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            refresh = response.status_code == 401 or data.get("code") in (99991663, 99991664, 99991668)
            if attempt < 3 and (refresh or response.status_code in (429, 502, 503, 504) or data.get("code") == 99991400):
                await asyncio.sleep(min(8, 0.5 * 2 ** attempt))
                continue
            if response.status_code >= 400 or data.get("code") != 0:
                raise SourceError(f"飞书读取失败（HTTP {response.status_code}，错误码 {data.get('code', '未知')}）。请检查权限、登录和表格授权。")
            return data.get("data", {})
        raise SourceError("飞书请求重试已耗尽。")

    async def _target(self, config):
        target = parse_target(config)
        if target["route"] == "wiki":
            data = await self._api(config, "/wiki/v2/spaces/get_node", {"token": target["token"]})
            node = data.get("node", {})
            if node.get("obj_type") != "bitable" or not node.get("obj_token"):
                raise SourceError("此 Wiki 链接不是多维表格，请复制表格自身链接。")
            target["token"] = node["obj_token"]
        return target

    async def _pages(self, config, path, params=None):
        params = dict(params or {})
        params.setdefault("page_size", 500)
        seen = set()
        while True:
            data = await self._api(config, path, params)
            yield data
            if not data.get("has_more", False):
                break
            token = data.get("page_token")
            if not token or token in seen:
                raise SourceError("飞书分页标记缺失或重复，已停止导入以避免漏行。")
            seen.add(token)
            params["page_token"] = token

    async def _api_metadata(self, config):
        target = await self._target(config)
        prefix = f"/bitable/v1/apps/{quote(target['token'], safe='')}/tables"
        tables = []
        async for page in self._pages(config, prefix):
            tables.extend({"id": t["table_id"], "name": t.get("name", t["table_id"])} for t in page.get("items", []))
        table_id = target["table_id"]
        if not table_id:
            if len(tables) != 1:
                return target, tables, [], None
            table_id = tables[0]["id"]
            target["table_id"] = table_id
        path = prefix + "/" + quote(table_id, safe="")
        fields = []
        async for page in self._pages(config, path + "/fields"):
            fields.extend({"id": f["field_id"], "name": f["field_name"], "type": f["type"]} for f in page.get("items", []))
        return target, tables, fields, path

    @staticmethod
    def _check_fields(config, fields):
        for key, label in (("image_field", "图片列"), ("log_field", "日志链接列")):
            if not any(config.get(key) in (f["id"], f["name"]) for f in fields):
                raise SourceError(f"请选择有效的{label}。")

    async def inspect(self, config: dict) -> dict:
        mode = config.get("mode", "api")
        if mode == "connector":
            state = self.connector_status(config.get("session_id", ""))
            if state["status"] != "ready":
                raise SourceError("请在飞书侧边栏连接器中完成数据传输后再预览。")
            self._check_connector_selection(config, state)
            with self._connector_db(state["session_id"]) as db:
                preview = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records ORDER BY ordinal LIMIT 5")]
            return {**state, "preview": preview, "tables": [{"id": state.get("table_id"), "name": "已导入表格"}]}
        if mode == "browser":
            return await self.browser.call("inspect", config=config)
        if mode != "api":
            raise SourceError("未知接入模式。")
        target, tables, fields, path = await self._api_metadata(config)
        if not path:
            return {"tables": tables, "fields": [], "total": None, "preview": [], "warnings": ["请选择数据表。"]}
        params = {"page_size": 5}
        if config.get("scope", "all") == "view":
            params["view_id"] = target["view_id"]
        page = await self._api(config, path + "/records", params)
        preview = [normalize_record(r, {**config, "table_id": target["table_id"]}, fields) for r in page.get("items", [])]
        return {"tables": tables, "table_id": target["table_id"], "fields": fields,
                "total": page.get("total"), "preview": preview, "warnings": [], "scope": config.get("scope", "all")}

    async def iter_records(self, config: dict) -> AsyncIterator[dict]:
        mode = config.get("mode", "api")
        if mode == "connector":
            state = self.connector_status(config.get("session_id", ""))
            if state["status"] != "ready":
                raise SourceError("连接器传输尚未完成。")
            self._check_connector_selection(config, state)
            with self._connector_db(state["session_id"]) as db:
                cursor = db.execute("SELECT payload FROM records ORDER BY ordinal")
                for (payload,) in cursor:
                    yield json.loads(payload)
            return
        if mode == "browser":
            response = await self.browser.call("start", config=config)
            reader_id = response["reader_id"]
            snapshot_dir = self.data_dir / "browser-snapshots" / self._safe_id(reader_id)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            downloads = asyncio.Semaphore(max(1, min(32, int(config.get("download_concurrency", 4)))))
            snapshots = {}
            async def snapshot_image(image):
                key = hashlib.sha256(str(image.get("token") or image.get("url") or "").encode()).hexdigest()
                async def fetch():
                    async with downloads:
                        path = snapshot_dir / key
                        try:
                            if path.is_file():
                                return str(path.resolve())
                            await self.browser.call("download", image=image, target=str(path.resolve()), config=config)
                            return str(path.resolve())
                        except SourceError:
                            return None
                if key not in snapshots:
                    snapshots[key] = asyncio.create_task(fetch())
                saved = await snapshots[key]
                if saved:
                    image["local_snapshot"] = saved
            try:
                while True:
                    # Private web URLs can expire while a large record manifest is
                    # importing. Cache each page's images before requesting the next.
                    snapshots.clear()
                    records = response.get("records", [])
                    active = {}
                    async def prepare(record):
                        await asyncio.gather(*(snapshot_image(image) for image in record.get("images", [])))
                    try:
                        upcoming = 0
                        for index, record in enumerate(records):
                            while upcoming < len(records) and upcoming < index + 4:
                                active[upcoming] = asyncio.create_task(prepare(records[upcoming]))
                                upcoming += 1
                            await active.pop(index)
                            yield record
                    finally:
                        for task in active.values():
                            task.cancel()
                        await asyncio.gather(*active.values(), return_exceptions=True)
                    if response["done"]:
                        break
                    response = await self.browser.call("next", reader_id=reader_id)
            finally:
                await self.browser.call("release", reader_id=reader_id)
            return
        target, tables, fields, path = await self._api_metadata(config)
        if not path:
            raise SourceError("链接未选择数据表，请选择具体数据表。")
        self._check_fields(config, fields)
        params = {}
        if config.get("scope", "all") == "view":
            params["view_id"] = target["view_id"]
        count = 0
        expected = None
        seen = set()
        async for page in self._pages(config, path + "/records", params):
            if expected is None:
                expected = page.get("total")
            elif page.get("total", expected) != expected:
                raise SourceError("导入期间表格行数发生变化，请重试以获得一致快照。")
            for raw in page.get("items", []):
                record = normalize_record(raw, {**config, "table_id": target["table_id"]}, fields)
                if record["record_id"] in seen:
                    raise SourceError("飞书分页出现重复记录，请重试。")
                seen.add(record["record_id"])
                count += 1
                yield record
        if expected is None or count != expected:
            raise SourceError(f"记录数校验失败：预期 {expected}，读取 {count}；未完成导入。")

    async def download_image(self, image: dict, target: Path, config: dict) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if image.get("error"):
            raise SourceError(str(image["error"]))
        if image.get("asset_id") and config.get("mode") == "connector":
            source = self._connector_path(config["session_id"]) / "assets" / self._safe_id(image["asset_id"])
            if not source.is_file():
                raise SourceError("连接器图片未完整上传，请重新配对导入。")
            await asyncio.to_thread(shutil.copyfile, source, target)
            return
        if config.get("mode") == "browser":
            if image.get("local_snapshot"):
                source = Path(image["local_snapshot"]).resolve()
                if source.is_relative_to((self.data_dir / "browser-snapshots").resolve()) and source.is_file():
                    await asyncio.to_thread(shutil.copyfile, source, target)
                    return
            await self.browser.call("download", image=image, target=str(target.resolve()), config=config)
            return
        headers = {}
        parameters = None
        if image.get("token"):
            base = parse_target(config)["api"]
            media_token = str(image["token"])
            url = base + "/drive/v1/medias/" + quote(media_token, safe="") + "/download"
            api_url = urlparse(url)
            # Advanced-permission Bases return `extra` in url/tmp_url. Only
            # consume these auth hints from the exact official media endpoint;
            # application credentials always go to our canonical API address.
            parameters = {}
            for candidate in (image.get("url"), image.get("tmp_url")):
                parsed = urlparse(candidate or "")
                trusted = parsed.scheme == "https" and parsed.netloc == api_url.netloc and not parsed.username
                query = parse_qs(parsed.query)
                matching = parsed.path == api_url.path or (
                    parsed.path == "/open-apis/drive/v1/medias/batch_get_tmp_download_url"
                    and media_token in query.get("file_tokens", []))
                if trusted and matching and query.get("extra"):
                    parameters["extra"] = query["extra"][0]
                    break
            permission = image.get("permission") or {}
            if "extra" not in parameters and all(permission.get(k) for k in ("tableId", "fieldId", "recordId")):
                parameters["extra"] = json.dumps({"bitablePerm": {"tableId": permission["tableId"],
                    "attachments": {permission["fieldId"]: {permission["recordId"]: [media_token]}}}}, separators=(",", ":"))
            headers["Authorization"] = "Bearer " + await self._token(config)
        else:
            url = image.get("url")
        if not url or urlparse(url).scheme not in ("http", "https"):
            raise SourceError("图片没有可用下载地址。")
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            async with self.client.stream("GET", url, headers=headers, params=parameters) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    async def login(self, url: str) -> dict:
        return await self.browser.call("login", url=url)

    async def browser_log(self, url: str, site: dict, target_dir: Path) -> dict:
        target_dir.mkdir(parents=True, exist_ok=True)
        lock = self.page_locks.setdefault(url, asyncio.Lock())
        async with lock:
            return await self.browser.call("log", url=url, site=site, target_dir=str(target_dir.resolve()))

    @staticmethod
    def _safe_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise SourceError("无效的连接器标识。")
        return value

    def _connector_path(self, session_id):
        return self.connector_dir / self._safe_id(session_id)

    @contextmanager
    def _connector_db(self, session_id):
        path = self._connector_path(session_id) / "records.sqlite3"
        if not path.is_file():
            raise SourceError("配对不存在，请创建新的配对。")
        database = sqlite3.connect(path)
        try:
            with database:
                yield database
        finally:
            database.close()

    def create_connector_session(self, config: dict) -> dict:
        session_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        path = self._connector_path(session_id)
        (path / "assets").mkdir(parents=True)
        state = {"session_id": session_id, "status": "waiting", "count": 0, "total": None,
                 "fields": [], "warnings": [], "config": {k: config.get(k) for k in ("url", "scope", "image_field", "log_field")},
                 "token_hash": hashlib.sha256(token.encode()).hexdigest(), "created": time.time()}
        (path / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        database = sqlite3.connect(path / "records.sqlite3")
        with database as db:
            db.execute("CREATE TABLE records (ordinal INTEGER PRIMARY KEY, record_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
        database.close()
        return {**{k: v for k, v in state.items() if k != "token_hash"}, "token": token}

    def _state(self, session_id, token=None):
        try:
            state = json.loads((self._connector_path(session_id) / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SourceError("配对不存在，请创建新的配对。") from exc
        if token is not None:
            if not secrets.compare_digest(state["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
                raise SourceError("连接器配对码不正确。")
            if time.time() - state["created"] > 24 * 3600:
                raise SourceError("连接器配对码已过期，请创建新的配对。")
        return state

    def _save_state(self, state):
        path = self._connector_path(state["session_id"]) / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def connector_status(self, session_id: str) -> dict:
        state = self._state(session_id)
        with self._connector_db(session_id) as db:
            state["count"] = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return {k: v for k, v in state.items() if k != "token_hash"}

    def authorize_connector(self, session_id: str, token: str) -> None:
        self._state(session_id, token)

    @staticmethod
    def _check_connector_selection(config: dict, state: dict) -> None:
        captured = state.get("config", {})
        scope = state.get("scope") or captured.get("scope")
        if config.get("scope") and scope and config["scope"] != scope:
            raise SourceError("连接器读取范围已固定；如需改变范围，请创建新配对重新导入。")
        for key, label in (("image_field", "图片列"), ("log_field", "日志列")):
            chosen = captured.get(key)
            field = next((f for f in state.get("fields", []) if chosen in (f["id"], f["name"])), None)
            if field and config.get(key) and config[key] not in (field["id"], field["name"]):
                raise SourceError(f"连接器{label}已固定为 {field['name']}；如需换列，请创建新配对。")

    def connector_metadata(self, session_id: str, token: str, metadata: dict) -> dict:
        state = self._state(session_id, token)
        if state["status"] != "waiting":
            raise SourceError("此配对已开始上传，请使用新配对重新导入。")
        total = metadata.get("total")
        if not isinstance(total, int) or total < 0:
            raise SourceError("连接器必须提供有效记录总数。")
        if not isinstance(metadata.get("fields"), list):
            raise SourceError("连接器字段格式错误。")
        state.update({k: metadata[k] for k in ("fields", "total", "table_id", "view_id", "scope") if k in metadata})
        state["config"].update({k: metadata[k] for k in ("image_field", "log_field", "scope") if k in metadata})
        state["status"] = "uploading"
        self._save_state(state)
        return {"status": state["status"], "count": 0}

    def connector_batch(self, session_id: str, token: str, offset: int, records: list[dict]) -> dict:
        state = self._state(session_id, token)
        if state["status"] != "uploading" or not isinstance(records, list) or len(records) > 200:
            raise SourceError("连接器状态或批次大小无效。")
        with self._connector_db(session_id) as db:
            count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if offset != count or count + len(records) > state["total"]:
                raise SourceError("连接器批次顺序或记录总数不一致。")
            for index, record in enumerate(records):
                if not record.get("record_id") or not isinstance(record.get("images"), list) or not isinstance(record.get("log_urls"), list):
                    raise SourceError("连接器记录格式错误。")
                for image in record["images"]:
                    if image.get("asset_id"):
                        self._safe_id(image["asset_id"])
                    # Local file paths are never trusted across the bridge.
                    image.pop("local_path", None)
                    image.pop("path", None)
                try:
                    db.execute("INSERT INTO records VALUES (?,?,?)", (offset + index, str(record["record_id"]), json.dumps(record, ensure_ascii=False)))
                except sqlite3.IntegrityError as exc:
                    raise SourceError("连接器出现重复记录，已拒绝整个批次。") from exc
        return {"status": "uploading", "count": count + len(records)}

    def write_connector_asset(self, session_id: str, token: str, asset_id: str, content=None, *, file_path: Path | None = None):
        state = self._state(session_id, token)
        if state["status"] != "uploading":
            raise SourceError("连接器未处于上传状态。")
        target = self._connector_path(session_id) / "assets" / self._safe_id(asset_id)
        if file_path is not None:
            shutil.copyfile(file_path, target)
        else:
            target.write_bytes(content)
        return {"asset_id": asset_id}

    def connector_finish(self, session_id: str, token: str, total: int) -> dict:
        state = self._state(session_id, token)
        with self._connector_db(session_id) as db:
            count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if state["status"] != "uploading" or total != state["total"] or count != total:
            raise SourceError("连接器记录数校验失败，传输未完成。")
        state.update(status="ready", count=count)
        self._save_state(state)
        return {"session_id": session_id, "status": "ready", "count": count, "total": total}
