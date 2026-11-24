"""GsOnebotFunPlugin 插件配置（支持 WebConsole 可视化编辑与轮询间隔热生效）。"""

from collections.abc import Callable

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

PLUGIN_DATA_DIR = get_res_path("GsOnebotFunPlugin")
CONFIG_PATH = PLUGIN_DATA_DIR / "config.json"

MIN_POLL_INTERVAL_MIN = 5
MAX_POLL_INTERVAL_MIN = 1440
DEFAULT_POLL_INTERVAL_MIN = 30

MIN_TIMEOUT_SEC = 5
MAX_TIMEOUT_SEC = 60
DEFAULT_TIMEOUT_SEC = 15

DEFAULT_API_BASE_URL = "https://api.iwara.tv"
DEFAULT_SITE_HOST = "www.iwara.tv"

_POLL_INTERVAL_KEY = "iwara_poll_interval_min"
_HTTP_SENSITIVE_KEYS = frozenset(
    {
        "iwara_proxy_url",
        "iwara_warmup_homepage",
        "iwara_request_timeout_sec",
        "iwara_cookie",
        "iwara_bearer_token",
        "iwara_login_email",
        "iwara_login_password",
        "iwara_api_base_url",
        "iwara_site_host",
    }
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "iwara_divider": GsDivider(
        title="Iwara 订阅设置",
        desc="控制 Iwara 博主更新巡检频率与反风控网络参数",
        data="Iwara 订阅设置",
    ),
    "iwara_poll_interval_min": GsIntConfig(
        title="Iwara 轮询间隔（分钟）",
        desc="定时巡检订阅博主更新的间隔，范围 5-1440 分钟，修改后自动热生效",
        data=DEFAULT_POLL_INTERVAL_MIN,
        max_value=MAX_POLL_INTERVAL_MIN,
    ),
    "iwara_proxy_url": GsStrConfig(
        title="Iwara 代理地址",
        desc="可选 HTTP/HTTPS/SOCKS 代理（如 http://127.0.0.1:7890），留空则直连",
        data="",
    ),
    "iwara_warmup_homepage": GsBoolConfig(
        title="请求前预热首页",
        desc="首次请求 API 前先访问 Iwara 首页建立浏览器会话，建议开启",
        data=True,
    ),
    "iwara_request_timeout_sec": GsIntConfig(
        title="请求超时时间（秒）",
        desc="单次网络请求超时时间，范围 5-60 秒",
        data=DEFAULT_TIMEOUT_SEC,
        max_value=MAX_TIMEOUT_SEC,
    ),
    "iwara_cookie": GsStrConfig(
        title="Iwara Cookie（可选）",
        desc="可选浏览器 Cookie（如包含 cf_clearance），留空则不发送",
        data="",
        secret=True,
    ),
    "iwara_bearer_token": GsStrConfig(
        title="Iwara Bearer Token（可选）",
        desc="可选浏览器访问令牌，留空时将按需尝试下方邮箱密码自动登录",
        data="",
        secret=True,
    ),
    "iwara_login_email": GsStrConfig(
        title="Iwara 登录邮箱（可选）",
        desc="配合登录密码自动获取并续期 Token，留空则匿名巡检",
        data="",
    ),
    "iwara_login_password": GsStrConfig(
        title="Iwara 登录密码（可选）",
        desc="与登录邮箱配合使用，留空则跳过自动登录",
        data="",
        secret=True,
    ),
    "iwara_api_base_url": GsStrConfig(
        title="Iwara API 地址",
        desc="默认 https://api.iwara.tv",
        data=DEFAULT_API_BASE_URL,
    ),
    "iwara_site_host": GsStrConfig(
        title="Iwara 站点域名",
        desc="用于 X-Site 请求头与推送链接，默认 www.iwara.tv",
        data=DEFAULT_SITE_HOST,
    ),
}

_on_poll_interval_change: list[Callable[[int], None]] = []
_on_http_config_change: list[Callable[[], None]] = []


def register_poll_interval_listener(callback: Callable[[int], None]) -> None:
    if callback not in _on_poll_interval_change:
        _on_poll_interval_change.append(callback)


def register_http_config_listener(callback: Callable[[], None]) -> None:
    if callback not in _on_http_config_change:
        _on_http_config_change.append(callback)


def get_iwara_poll_interval_min() -> int:
    raw = PLUGIN_CONFIG.get_config(_POLL_INTERVAL_KEY).data
    value = raw if isinstance(raw, int) else DEFAULT_POLL_INTERVAL_MIN
    return max(MIN_POLL_INTERVAL_MIN, min(MAX_POLL_INTERVAL_MIN, value))


def get_iwara_timeout_sec() -> int:
    raw = PLUGIN_CONFIG.get_config("iwara_request_timeout_sec").data
    value = raw if isinstance(raw, int) else DEFAULT_TIMEOUT_SEC
    return max(MIN_TIMEOUT_SEC, min(MAX_TIMEOUT_SEC, value))


def get_iwara_str(key: str, default: str = "") -> str:
    raw = PLUGIN_CONFIG.get_config(key).data
    if isinstance(raw, str):
        text = raw.strip()
        return text if text else default
    return default


def get_iwara_bool(key: str, default: bool = True) -> bool:
    raw = PLUGIN_CONFIG.get_config(key).data
    return raw if isinstance(raw, bool) else default


class GsOnebotFunConfig(StringConfig):
    """WebConsole 保存配置时同步触发轮询任务重载与客户端会话刷新。"""

    def set_config(
        self,
        key: str,
        value: str | list[str] | list[int] | bool | dict[str, list[str]] | int | float,
    ) -> bool:
        ok = super().set_config(key, value)
        if not ok:
            return False
        if key == _POLL_INTERVAL_KEY:
            minutes = get_iwara_poll_interval_min()
            for callback in _on_poll_interval_change:
                callback(minutes)
        elif key in _HTTP_SENSITIVE_KEYS:
            for callback in _on_http_config_change:
                callback()
        return True


PLUGIN_CONFIG = GsOnebotFunConfig("GsOnebotFunPlugin", CONFIG_PATH, CONFIG_DEFAULT)
