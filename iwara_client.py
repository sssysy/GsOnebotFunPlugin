"""Iwara 全异步反风控 HTTP 客户端（基于 curl_cffi AsyncSession）。"""

from __future__ import annotations

import re
import json
import time
import asyncio
from pathlib import Path
from dataclasses import dataclass

import aiofiles
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import CurlError, RequestException

from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown

from .config import (
    PLUGIN_DATA_DIR,
    DEFAULT_SITE_HOST,
    DEFAULT_API_BASE_URL,
    get_iwara_str,
    get_iwara_bool,
    get_iwara_timeout_sec,
    register_http_config_listener,
)

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

_TOKEN_FILE: Path = PLUGIN_DATA_DIR / "iwara_token.json"
_REFRESH_TOKEN_KEY = "iwara_refresh_token"
_TOKEN_TTL_SEC = 3600.0
_TOKEN_REFRESH_MARGIN_SEC = 120.0
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CF_KEYWORDS = (
    "cloudflare",
    "cf-chl",
    "just a moment",
    "cf-mitigated",
    "turnstile",
    "challenges.cloudflare.com",
)


@dataclass(frozen=True)
class IwaraUserProfile:
    user_id: str
    username: str
    display_name: str


@dataclass(frozen=True)
class IwaraMediaItem:
    item_id: str
    title: str


class IwaraApiError(RuntimeError):
    """Iwara API 请求或响应异常。"""


class CloudflareBlockedError(IwaraApiError):
    """请求触发 Cloudflare 人机校验拦截。"""


def _is_cf_text(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in _CF_KEYWORDS)


def _looks_binary(text: str) -> bool:
    if not text:
        return False
    sample = text[:200]
    bad = 0
    for ch in sample:
        code = ord(ch)
        if code < 9 or (13 < code < 32) or code == 65533:
            bad += 1
    return bad > max(8, len(sample) // 8)


def _format_http_error(status: int, content: str, proxy_used: bool) -> str:
    body = content.strip()
    if status == 403 and (_is_cf_text(body) or _looks_binary(body)):
        has_cookie = bool(get_iwara_str("iwara_cookie"))
        return (
            "HTTP 403 (Cloudflare 拦截)。请检查代理配置（iwara_proxy_url）或补充 "
            f"iwara_cookie（含 cf_clearance）。当前代理：{'已启用' if proxy_used else '未启用'}，"
            f"Cookie：{'已配置' if has_cookie else '未配置'}。"
        )
    clean = _HTML_TAG_RE.sub(" ", body)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > 220:
        clean = clean[:220] + "..."
    return f"HTTP {status}: {clean or 'empty response'}"


def _json_str(data: JsonObject, key: str, default: str = "") -> str:
    if key not in data:
        return default
    raw = data[key]
    if raw is None:
        return default
    if isinstance(raw, str):
        text = raw.strip()
        return text if text else default
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return default


def _extract_item_objects(data: JsonValue) -> list[JsonObject]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "items", "data", "list"):
        if key not in data:
            continue
        val = data[key]
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
        if isinstance(val, dict):
            nested = _extract_item_objects(val)
            if nested:
                return nested
    return []


def _parse_json_body(text: str) -> JsonValue:
    # 不可信第三方 HTTP 响应体：解析失败直接转为明确的业务异常
    try:
        parsed: JsonValue = json.loads(text)
        return parsed
    except json.JSONDecodeError as exc:
        raise IwaraApiError(f"Iwara 返回了非 JSON 响应：{text[:120]}") from exc


class IwaraClient:
    """基于 curl_cffi 浏览器指纹伪装的异步 Iwara 客户端。"""

    def __init__(self) -> None:
        self._session: AsyncSession | None = None
        self._session_lock = asyncio.Lock()
        self._warmup_done = False
        self._warmup_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._access_token = ""
        self._refresh_token = ""
        self._token_expiry = 0.0
        self._refresh_loaded = False
        self._dirty = False

    def mark_config_dirty(self) -> None:
        """网络或鉴权配置变更后标记会话失效，下次请求自动重建。"""
        self._dirty = True
        self._warmup_done = False
        self._access_token = ""
        self._token_expiry = 0.0

    async def _get_session(self) -> AsyncSession:
        async with self._session_lock:
            if self._dirty and self._session is not None:
                old = self._session
                self._session = None
                self._dirty = False
                try:
                    await old.close()
                except Exception as exc:
                    logger.warning(f"[GsOnebotFunPlugin] 关闭旧 curl_cffi 会话失败: {exc}")
            if self._session is None:
                self._dirty = False
                self._session = AsyncSession(impersonate="chrome")
            return self._session

    async def close(self) -> None:
        async with self._session_lock:
            if self._session is not None:
                session = self._session
                self._session = None
                try:
                    await session.close()
                except Exception as exc:
                    logger.warning(f"[GsOnebotFunPlugin] 关闭 curl_cffi 会话失败: {exc}")
            self._warmup_done = False

    def _site_host(self) -> str:
        raw = get_iwara_str("iwara_site_host", DEFAULT_SITE_HOST)
        host = raw.removeprefix("https://").removeprefix("http://").strip("/")
        return host or DEFAULT_SITE_HOST

    def _api_bases(self) -> list[str]:
        configured = get_iwara_str("iwara_api_base_url", DEFAULT_API_BASE_URL).rstrip("/")
        candidates = [configured, "https://api.iwara.tv", "https://apiq.iwara.tv"]
        seen: set[str] = set()
        result: list[str] = []
        for base in candidates:
            if base and base not in seen:
                seen.add(base)
                result.append(base)
        return result

    def _proxy(self) -> str | None:
        raw = get_iwara_str("iwara_proxy_url")
        return raw if raw else None

    def _base_headers(self) -> dict[str, str]:
        host = self._site_host()
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://{host}/",
            "Origin": f"https://{host}",
            "X-Site": host,
        }
        cookie = get_iwara_str("iwara_cookie")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _ensure_warmup(self, session: AsyncSession, proxy: str | None) -> None:
        if self._warmup_done or not get_iwara_bool("iwara_warmup_homepage", True):
            return
        async with self._warmup_lock:
            if self._warmup_done:
                return
            host = self._site_host()
            timeout_sec = min(15, get_iwara_timeout_sec())
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"https://{host}/",
            }
            cookie = get_iwara_str("iwara_cookie")
            if cookie:
                headers["Cookie"] = cookie
            try:
                await session.request(
                    "GET",
                    f"https://{host}/",
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout_sec,
                )
            except (RequestException, CurlError, OSError) as exc:
                logger.debug(f"[GsOnebotFunPlugin] Iwara 首页预热失败（已忽略）: {exc}")
            finally:
                self._warmup_done = True

    async def _load_refresh_token(self) -> None:
        if self._refresh_loaded:
            return
        self._refresh_loaded = True
        try:
            async with aiofiles.open(_TOKEN_FILE, "r", encoding="utf-8") as fp:
                raw_text = await fp.read()
            if not raw_text.strip():
                return
            data = _parse_json_body(raw_text)
            if isinstance(data, dict):
                self._refresh_token = _json_str(data, _REFRESH_TOKEN_KEY)
        except FileNotFoundError:
            return
        except (OSError, IwaraApiError) as exc:
            logger.warning(f"[GsOnebotFunPlugin] 读取 Iwara refreshToken 失败: {exc}")

    async def _save_refresh_token(self, token: str) -> None:
        self._refresh_token = token
        try:
            await asyncio.to_thread(_TOKEN_FILE.parent.mkdir, parents=True, exist_ok=True)
            payload = json.dumps({_REFRESH_TOKEN_KEY: token}, ensure_ascii=False, indent=2)
            async with aiofiles.open(_TOKEN_FILE, "w", encoding="utf-8") as fp:
                await fp.write(payload)
        except OSError as exc:
            logger.warning(f"[GsOnebotFunPlugin] 持久化 Iwara refreshToken 失败: {exc}")

    async def _clear_refresh_token(self) -> None:
        self._refresh_token = ""
        try:
            await asyncio.to_thread(_TOKEN_FILE.unlink, True)
        except OSError as exc:
            logger.warning(f"[GsOnebotFunPlugin] 清理 Iwara refreshToken 失败: {exc}")

    async def _get_bearer_token(self) -> str:
        manual = get_iwara_str("iwara_bearer_token")
        if manual:
            return manual
        email = get_iwara_str("iwara_login_email")
        password = get_iwara_str("iwara_login_password")
        if not email or not password:
            return ""

        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expiry - _TOKEN_REFRESH_MARGIN_SEC:
                return self._access_token
            await self._load_refresh_token()
            if self._refresh_token:
                refreshed = await self._refresh_access_token()
                if refreshed:
                    self._access_token = refreshed
                    self._token_expiry = time.time() + _TOKEN_TTL_SEC
                    return refreshed
            logged_in = await self._login_for_token(email, password)
            if logged_in:
                self._access_token = logged_in
                self._token_expiry = time.time() + _TOKEN_TTL_SEC
                return logged_in
            return ""

    async def _refresh_access_token(self) -> str:
        base = self._api_bases()[0]
        try:
            data = await self._request_json(
                "POST",
                f"{base}/user/token",
                extra_headers={"Authorization": f"Bearer {self._refresh_token}"},
                skip_auth=True,
            )
        except IwaraApiError as exc:
            logger.warning(f"[GsOnebotFunPlugin] Iwara token 刷新失败，将尝试重新登录: {exc}")
            await self._clear_refresh_token()
            return ""
        if isinstance(data, dict):
            token = _json_str(data, "accessToken") or _json_str(data, "token")
            if token:
                return token
        await self._clear_refresh_token()
        return ""

    async def _login_for_token(self, email: str, password: str) -> str:
        base = self._api_bases()[0]
        body = json.dumps({"email": email, "password": password})
        try:
            data = await self._request_json(
                "POST",
                f"{base}/user/login",
                extra_headers={"Content-Type": "application/json"},
                body=body,
                skip_auth=True,
            )
        except IwaraApiError as exc:
            logger.warning(f"[GsOnebotFunPlugin] Iwara 自动登录失败: {exc}")
            return ""
        if not isinstance(data, dict):
            return ""
        refresh = _json_str(data, "refreshToken") or _json_str(data, "refresh_token")
        if refresh:
            await self._save_refresh_token(refresh)
        return _json_str(data, "accessToken") or _json_str(data, "token")

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        body: str | None = None,
        skip_auth: bool = False,
    ) -> JsonValue:
        headers = self._base_headers()
        if not skip_auth:
            bearer = await self._get_bearer_token()
            if bearer:
                headers["Authorization"] = bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
        if extra_headers:
            headers.update(extra_headers)

        session = await self._get_session()
        proxy = self._proxy()
        await self._ensure_warmup(session, proxy)
        timeout_sec = get_iwara_timeout_sec()

        try:
            if method == "POST":
                resp = await session.request(
                    "POST",
                    url,
                    params=params,
                    headers=headers,
                    data=body,
                    proxy=proxy,
                    timeout=timeout_sec,
                )
            else:
                resp = await session.request(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout_sec,
                )
        except (RequestException, CurlError, OSError) as exc:
            raise IwaraApiError(f"网络请求失败：{exc}") from exc

        status = int(resp.status_code)
        text = resp.text
        if status >= 400:
            msg = _format_http_error(status, text, proxy is not None)
            if status == 403 and (_is_cf_text(text) or _looks_binary(text)):
                raise CloudflareBlockedError(msg)
            raise IwaraApiError(msg)
        return _parse_json_body(text)

    async def get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> JsonValue:
        bases = self._api_bases()
        last_exc: Exception | None = None
        for idx, base in enumerate(bases):
            url = f"{base}/{path.lstrip('/')}"
            try:
                return await self._request_json("GET", url, params=params)
            except CloudflareBlockedError:
                raise
            except IwaraApiError as exc:
                last_exc = exc
                if idx < len(bases) - 1 and self._is_retryable(str(exc)):
                    logger.warning(f"[GsOnebotFunPlugin] Iwara 请求 {base} 失败，尝试备用节点: {exc}")
                    continue
                raise
        raise IwaraApiError(f"请求失败：{last_exc}")

    @staticmethod
    def _is_retryable(error_text: str) -> bool:
        lower = error_text.lower()
        return any(
            token in lower for token in ("timeout", "timed out", "connection", "502", "503", "504", "network", "ssl")
        )

    async def get_user_profile(self, username: str) -> IwaraUserProfile | None:
        data = await self.get_json(f"/profile/{username}")
        if not isinstance(data, dict):
            return None
        user_obj: JsonObject = data
        if "user" in data:
            nested_user = data["user"]
            if isinstance(nested_user, dict):
                user_obj = nested_user
        user_id = _json_str(user_obj, "id")
        if not user_id:
            return None
        canonical_username = _json_str(user_obj, "username", username)
        display_name = _json_str(user_obj, "name", canonical_username)
        return IwaraUserProfile(
            user_id=user_id,
            username=canonical_username,
            display_name=display_name,
        )

    async def get_user_videos(self, user_id: str, limit: int = 10) -> list[IwaraMediaItem]:
        data = await self.get_json(
            "/videos",
            params={
                "sort": "date",
                "rating": "all",
                "user": user_id,
                "limit": str(limit),
            },
        )
        return self._to_media_items(data, "新视频")

    async def get_user_images(self, user_id: str, limit: int = 10) -> list[IwaraMediaItem]:
        data = await self.get_json(
            "/images",
            params={
                "sort": "date",
                "rating": "all",
                "user": user_id,
                "limit": str(limit),
            },
        )
        return self._to_media_items(data, "新图片")

    @staticmethod
    def _to_media_items(data: JsonValue, default_title: str) -> list[IwaraMediaItem]:
        items: list[IwaraMediaItem] = []
        for raw in _extract_item_objects(data):
            item_id = _json_str(raw, "id")
            if not item_id:
                continue
            title = _json_str(raw, "title", default_title)
            items.append(IwaraMediaItem(item_id=item_id, title=title))
        return items


iwara_client = IwaraClient()
register_http_config_listener(iwara_client.mark_config_dirty)


@on_core_shutdown
async def _close_iwara_client() -> None:
    await iwara_client.close()
