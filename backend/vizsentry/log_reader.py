"""Stream original logs to disk; never claim a generic web page is the full log."""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import re
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


class _HTMLText(HTMLParser):
    def __init__(self, writer):
        super().__init__(convert_charrefs=True)
        self.writer = writer
        self.skip = 0
        self.has_password = False
        self.sample = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
        if tag == "input" and attributes.get("type", "").lower() == "password":
            self.has_password = True
        if tag in {"br", "p", "div", "tr", "li", "pre", "h1", "h2", "h3"} and not self.skip:
            self.writer.write("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "tr", "li", "pre"} and not self.skip:
            self.writer.write("\n")

    def handle_data(self, data):
        if not self.skip:
            self.writer.write(data)
            if len(self.sample) < 16000:
                self.sample += data[:16000 - len(self.sample)]


def _encoding(sample: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)", content_type, re.I)
    candidates = [match.group(1)] if match else []
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    candidates.extend(["utf-8", "gb18030"])
    for candidate in candidates:
        try:
            decoder = codecs.getincrementaldecoder(candidate)(errors="strict")
            decoder.decode(sample, final=False)
            return candidate
        except (UnicodeError, LookupError):
            continue
    return "utf-8"


def _normalize(raw_path: Path, text_path: Path, content_type: str) -> dict:
    with raw_path.open("rb") as source:
        sample = source.read(65536)
        source.seek(0)
        encoding = _encoding(sample, content_type)
        is_html = "html" in content_type.lower() or sample.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        replacements = 0
        with text_path.open("w", encoding="utf-8", newline="") as output:
            parser = _HTMLText(output) if is_html else None
            while block := source.read(65536):
                text = decoder.decode(block)
                replacements += text.count("\ufffd")
                if parser:
                    parser.feed(text)
                else:
                    output.write(text)
            remainder = decoder.decode(b"", final=True)
            replacements += remainder.count("\ufffd")
            if parser:
                parser.feed(remainder)
                parser.close()
            else:
                output.write(remainder)
    login = bool(parser and (parser.has_password or re.search(
        r"扫码登录|扫描二维码登录|请先登录|sign in to continue|log in to continue", parser.sample, re.I)))
    return {"encoding": encoding, "is_html": is_html, "needs_login": login,
            "replacement_characters": replacements}


def _login_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(re.search(r"(?:^|[/_.-])(login|signin|passport|sso)(?:$|[/_.?-])", parsed.netloc + parsed.path, re.I))


def _site_headers(site: dict) -> dict:
    headers = {"User-Agent": "VizSentry/0.1 (local log reader)", "Accept-Encoding": "identity"}
    token = site.get("token")
    if token:
        name = str(site.get("header") or site.get("token_header") or "Authorization")
        value = str(token)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("站点令牌或请求头名称格式无效")
        if name.lower() == "authorization" and not re.match(r"^[A-Za-z]+\s", value):
            value = "Bearer " + value
        headers[name] = value
    return headers


async def _browser_result(url: str, target_dir: Path, site: dict, callback) -> dict:
    try:
        received = await callback(url, site, target_dir)
    except Exception as exc:
        return {"url": url, "name": "日志", "path": None, "text_path": None,
                "status": "error", "complete": False, "size": 0, "error": f"浏览器读取失败：{exc}"}
    status = {"complete": "ready", "login_required": "needs_login", "failed": "error"}.get(received.get("status"), received.get("status", "error"))
    raw_path = received.get("raw_path") or received.get("path")
    text_path = received.get("text_path")
    content_type = received.get("content_type", "")
    error = received.get("error")
    name = received.get("name") or "网页日志"
    try:
        # Authenticated downloads are original bytes and may not be UTF-8.
        if raw_path and (not text_path or str(raw_path) == str(text_path)) and status in {"ready", "partial"}:
            text_path = str(target_dir / ("normalized-" + uuid.uuid4().hex[:8] + ".txt"))
            normalized = await asyncio.to_thread(_normalize, Path(raw_path), Path(text_path), content_type)
            if normalized["needs_login"]:
                status, error = "needs_login", "下载结果是登录页面，请登录后重试"
            elif normalized["is_html"] or normalized["replacement_characters"]:
                status = "partial"
                error = error or "下载内容可能不完整或编码损坏，原始字节已保存"
        raw_paths = received.get("raw_paths") or []
        if len(raw_paths) > 1:
            archive = target_dir / ("original-pages-" + uuid.uuid4().hex[:8] + ".zip")
            def bundle():
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                    for index, path in enumerate(raw_paths):
                        output.write(path, arcname=f"{index + 1:04d}-{Path(path).name}")
            await asyncio.to_thread(bundle)
            raw_path, name = str(archive), "网页日志原始页面.zip"
        size = Path(raw_path).stat().st_size if raw_path and Path(raw_path).is_file() else 0
    except (OSError, ValueError) as exc:
        status, error, size = "partial", f"浏览器日志归档或文本处理失败：{exc}", 0
    # Keep adapter-local page paths out of the public row payload.
    return {"url": url, "name": name, "path": raw_path, "text_path": text_path,
            "status": status, "complete": status == "ready", "size": size,
            "content_type": content_type, "error": error}


async def fetch_log(url: str, target_dir: Path, site: dict | None = None, browser_fetch=None) -> dict:
    """Download direct text/JSON/files, delegating authenticated/structured pages."""
    site = dict(site or {})
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    result = {"url": url, "name": "日志", "path": None, "text_path": None,
              "status": "error", "complete": False, "size": 0}
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return {**result, "error": "日志链接必须是有效的 http(s) URL，且不能包含账号密码"}
    except ValueError:
        return {**result, "error": "日志链接格式错误"}
    try:
        headers = _site_headers(site)
    except ValueError as exc:
        return {**result, "error": str(exc)}
    if browser_fetch and any(site.get(key) for key in ("body_selector", "next_selector", "download_selector", "completion_selector", "use_browser")):
        return await _browser_result(url, target_dir, site, browser_fetch)
    # Unique download names prevent two row retries from observing half-written files.
    basename = hashlib.sha256(url.encode()).hexdigest()[:16] + "-" + uuid.uuid4().hex[:8]
    raw_path = target_dir / (basename + ".original")
    text_path = target_dir / (basename + ".txt")
    result.update({"name": Path(parsed.path).name or "日志", "path": str(raw_path), "text_path": str(text_path)})
    current_url = url
    content_type = ""
    received_bytes = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20), follow_redirects=False) as client:
            for _ in range(11):
                for attempt in range(3):
                    async with client.stream("GET", current_url, headers=headers) as response:
                        if response.status_code == 429 or response.status_code >= 500:
                            if attempt < 2:
                                await asyncio.sleep(2 ** attempt)
                                continue
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                return {**result, "path": None, "text_path": None, "error": "日志重定向没有目标地址"}
                            next_url = urljoin(current_url, location)
                            next_parts = urlsplit(next_url)
                            if next_parts.scheme not in {"http", "https"} or not next_parts.hostname:
                                return {**result, "path": None, "text_path": None, "error": "日志重定向不是 HTTP 地址"}
                            if _login_url(next_url):
                                if browser_fetch:
                                    return await _browser_result(url, target_dir, site, browser_fetch)
                                return {**result, "path": None, "text_path": None, "status": "needs_login", "error": "日志站点要求登录"}
                            # Tokens must never follow a redirect to a different origin.
                            before = urlsplit(current_url)
                            if (before.scheme, before.hostname, before.port) != (next_parts.scheme, next_parts.hostname, next_parts.port):
                                headers = {"User-Agent": headers["User-Agent"], "Accept-Encoding": "identity"}
                            current_url = next_url
                            break
                        if response.status_code in (401, 403):
                            if browser_fetch:
                                return await _browser_result(url, target_dir, site, browser_fetch)
                            return {**result, "path": None, "text_path": None, "status": "needs_login", "error": f"日志访问需要登录或权限（HTTP {response.status_code}）"}
                        if not response.is_success:
                            return {**result, "path": None, "text_path": None, "error": f"日志下载返回 HTTP {response.status_code}"}
                        content_type = response.headers.get("content-type", "text/plain")
                        expected = response.headers.get("content-length")
                        with raw_path.open("wb") as output:
                            async for chunk in response.aiter_bytes(65536):
                                output.write(chunk)
                                received_bytes += len(chunk)
                        result["size"] = received_bytes
                        normalized = await asyncio.to_thread(_normalize, raw_path, text_path, content_type)
                        result.update({"encoding": normalized["encoding"], "content_type": content_type,
                                       "final_url": current_url})
                        if normalized["needs_login"] or _login_url(current_url):
                            if browser_fetch:
                                return await _browser_result(url, target_dir, site, browser_fetch)
                            return {**result, "status": "needs_login", "error": "日志页面要求登录，保存的是登录页面"}
                        if expected and expected.isdigit() and not response.headers.get("content-encoding") and int(expected) != received_bytes:
                            return {**result, "status": "partial", "error": "下载字节数与服务器声明不一致；原始片段已保留"}
                        if normalized["replacement_characters"]:
                            return {**result, "status": "partial", "error": "部分文本无法按声明编码解码；原始字节完整保留"}
                        if normalized["is_html"]:
                            if browser_fetch:
                                return await _browser_result(url, target_dir, site, browser_fetch)
                            return {**result, "status": "partial", "error": "已提取网页正文，但无法确认分页或动态内容完整；请配置站点规则"}
                        return {**result, "status": "ready", "complete": True}
            return {**result, "path": None, "text_path": None, "error": "日志重定向次数超过上限"}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        error = "日志下载失败：" + str(exc)
        if raw_path.exists() and raw_path.stat().st_size:
            try:
                await asyncio.to_thread(_normalize, raw_path, text_path, content_type)
                return {**result, "status": "partial", "size": raw_path.stat().st_size,
                        "error": error + "；已保存下载片段"}
            except (OSError, ValueError):
                pass
        return {**result, "path": None, "text_path": None, "error": error}
