"""
MoviePilot 宿主模块的最小桩实现。

插件运行在 MoviePilot 内，直接 import 宿主模块；为了让插件核心逻辑（路径映射、
事件名过滤、后缀无关匹配、演练流程）能脱离宿主做单元测试，这里用最小实现把
``app.*`` 装入 ``sys.modules``。

只桩掉测试真正用到的部分，不做行为模拟之外的任何假设。
"""

import sys
import types
from enum import Enum
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins.v3" / "embysyncdel" / "__init__.py"


def _module(name, **attrs):
    """创建并注册一个桩模块。"""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class MediaSource(str, Enum):
    """媒体信息来源。"""

    TMDB = "tmdb"
    DOUBAN = "douban"
    IMDB = "imdb"


class MediaType(str, Enum):
    """媒体类型。"""

    MOVIE = "电影"
    TV = "电视剧"


class MessageType(str, Enum):
    """消息类型。"""

    Plugin = "Plugin"
    Resource = "Resource"


class EventType(str, Enum):
    """事件类型。"""

    WebhookMessage = "webhook_message"


class MediaImageType(str, Enum):
    """图片类型。"""

    Backdrop = "backdrop"
    Poster = "poster"


class _Settings:
    """宿主配置桩。"""

    RMT_MEDIAEXT = [".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".m2ts"]
    RMT_SUBEXT = [".srt", ".ass"]
    API_TOKEN = "test-token"
    MP_DOMAIN = staticmethod(lambda path="": f"http://localhost{path}")


class _Logger:
    """日志桩：把日志收进列表，便于断言。"""

    def __init__(self):
        self.records = []

    def _log(self, level, message):
        self.records.append((level, message))

    def info(self, message):
        self._log("info", message)

    def warning(self, message):
        self._log("warning", message)

    def error(self, message):
        self._log("error", message)

    def debug(self, message):
        self._log("debug", message)


logger = _Logger()


class _EventManager:
    """事件管理器桩：注册即记录，不做分发。"""

    def __init__(self):
        self.handlers = {}

    def register(self, event_type):
        def decorator(func):
            self.handlers.setdefault(event_type, []).append(func)
            return func

        return decorator


eventmanager = _EventManager()


class Event:
    """事件桩。"""

    def __init__(self, event_data=None, event_type=None):
        self.event_data = event_data
        self.event_type = event_type


class WebhookEventInfo:
    """媒体服务器事件报文桩（只保留插件实际读取的字段）。"""

    def __init__(self, event=None, item_name=None, item_path=None, media_type=None,
                 item_type=None, media_source=None, media_id=None, item_id=None,
                 season_id=None, episode_id=None, json_object=None, item_isvirtual=False):
        self.event = event
        self.item_name = item_name
        self.item_path = item_path
        self.media_type = media_type
        self.item_type = item_type
        self.media_source = media_source
        self.media_id = media_id
        self.item_id = item_id
        self.season_id = season_id
        self.episode_id = episode_id
        self.json_object = json_object or {}
        self.item_isvirtual = item_isvirtual


class Response:
    """接口返回桩。"""

    def __init__(self, success=True, message="", data=None):
        self.success = success
        self.message = message
        self.data = data


class _PluginBase:
    """插件基类桩：只实现插件用到的宿主能力。"""

    def __init__(self):
        self.chain = None
        self._data = {}
        self._config = {}
        self.messages = []

    # --- 数据存取 ---
    def save_data(self, key, value):
        self._data[key] = value

    def get_data(self, key=None, plugin_id=None):
        if plugin_id and plugin_id != self.__class__.__name__:
            return None
        return self._data.get(key)

    def del_data(self, key=None):
        self._data.pop(key, None)

    def update_config(self, config):
        self._config = dict(config or {})

    # --- 消息 ---
    def post_message(self, **kwargs):
        self.messages.append(kwargs)


class TransferHistoryFilter:
    """整理记录筛选桩：只保存字段，便于断言查询条件。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


class QueryPageRequest:
    """分页请求桩。"""

    def __init__(self, page=1, count=20, **kwargs):
        self.page = page
        self.count = count


class TransferHistoryOper:
    """整理记录操作桩（默认返回空）。"""

    def query(self, filters, page):
        return [], 0

    def delete(self, historyid):
        return True

    def get_by_media_identity(self, media_source, media_id, mtype=None):
        return None


class DownloadHistoryOper:
    """下载记录操作桩。"""

    def get_files_by_hash(self, download_hash):
        return []


def resolve_media_identity(obj):
    """身份解析桩：读 media_source / media_id。"""
    return getattr(obj, "media_source", None), getattr(obj, "media_id", None)


class MediaServerIdentityHelper:
    """媒体身份助手桩：从提供方 ID 解析 TMDB 身份。"""

    @staticmethod
    def from_provider_ids(provider_ids):
        if not provider_ids:
            return None, None
        tmdb_id = provider_ids.get("Tmdb")
        if tmdb_id:
            return MediaSource.TMDB, str(tmdb_id)
        return None, None


class DownloaderHelper:
    """下载器助手桩。"""

    def get_services(self):
        return {}

    def is_downloader(self, name):
        return False


class SystemUtils:
    """系统工具桩。"""

    @staticmethod
    def is_windows():
        return False


def install():
    """把桩模块注册进 sys.modules（幂等）。"""
    if "app" in sys.modules and getattr(sys.modules["app"], "__stub__", False):
        return

    app = _module("app", __stub__=True)
    app.schemas = _module(
        "app.schemas",
        WebhookEventInfo=WebhookEventInfo,
        Response=Response,
    )

    _module("app.plugins", _PluginBase=_PluginBase)
    _module("app.db", __path__=[])
    _module("app.db.oper", __path__=[])
    _module("app.db.oper.transferhistory", TransferHistoryOper=TransferHistoryOper)
    _module("app.db.oper.downloadhistory", DownloadHistoryOper=DownloadHistoryOper)
    _module(
        "app.schemas.query",
        TransferHistoryFilter=TransferHistoryFilter,
        QueryPageRequest=QueryPageRequest,
    )
    _module(
        "app.schemas.types",
        EventType=EventType,
        MediaImageType=MediaImageType,
        MediaSource=MediaSource,
        MediaType=MediaType,
        MessageType=MessageType,
    )
    _module("app.sdk", __path__=[])
    _module("app.sdk.config", settings=_Settings())
    _module("app.sdk.events", Event=Event, eventmanager=eventmanager)
    _module("app.sdk.logging", logger=logger)
    _module("app.sdk.media", resolve_media_identity=resolve_media_identity)
    _module(
        "app.sdk.services",
        DownloaderHelper=DownloaderHelper,
        MediaServerIdentityHelper=MediaServerIdentityHelper,
    )
    _module("app.sdk.utilities", SystemUtils=SystemUtils)


def load_plugin():
    """加载插件模块（自动先装桩）。"""
    import importlib.util

    install()
    spec = importlib.util.spec_from_file_location("embysyncdel", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
