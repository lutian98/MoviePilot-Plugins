"""
Emby 联动删除（EmbySyncDel）

Emby 删除影片 / 剧集后，同步清理 MoviePilot 侧的整理记录、源文件、媒体库文件与下载任务。

与同类插件的区别（本插件存在的理由）：

1. **事件名可配置** —— 同类插件把事件名写死在代码里（如只认 ``library.deleted``），
   媒体服务器换一个事件名就整体静默失效。本插件默认同时接受
   ``library.deleted`` / ``ItemDeleted`` / ``item.deleted``，并允许自定义。
2. **路径后缀无关匹配** —— 同类插件要求「媒体服务器上报的路径」在替换前缀后与整理记录的
   ``dest`` **完全相等**。当媒体库是 strm / 软链接架构时，媒体服务器上报
   ``…/影片 - 1080p.strm``，而 MoviePilot 记录里是 ``…/影片 - 1080p.mkv``，
   后缀不同 → 永远查不到整理记录 → 静默放弃。本插件按 **媒体身份 + 目录 / 文件名主干**
   匹配，与后缀无关。
3. **媒体身份三级兜底** —— 插件事件 → 提供方 ID（ProviderIds）→ 手动指定，
   任一层拿到来源与原生 ID 即可继续，避免"身份缺失即整体失效"。
4. **默认只出清单** —— ``dry_run`` 默认开启：只发通知列出将要删除的内容，不动任何数据。

License: GPL-3.0
"""

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.schemas.query import QueryPageRequest, TransferHistoryFilter
from app.schemas.types import EventType, MediaSource, MessageType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import resolve_media_identity
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper, MediaServerIdentityHelper


# 默认接受的事件名（Emby 各版本 / 各通知插件使用的删除事件名并不统一）
DEFAULT_EVENT_TYPES = "library.deleted,ItemDeleted,item.deleted"

# 容器级条目类型：这些事件指的是「文件夹 / 剧集容器」，不是具体影片文件。
# 实测这类事件大量来自整理换版重建与其它媒体库的清理动作，按目录名匹配极易误删仍在库里的影片。
CONTAINER_ITEM_TYPES = {
    "folder", "series", "season", "collectionfolder", "boxset", "collection",
}
# 下载任务处理方式
ACTION_DELETE = "delete"
ACTION_STOP = "stop"
# 辅种递归处理的最大深度，防止环路
MAX_SEED_DEPTH = 5
# 插件自带历史最多保留条数
MAX_HISTORY = 200
# 整理记录的查询上限（同一部片的多版本记录通常只有几条）
RECORD_QUERY_PAGE_SIZE = 200


# --------------------------------------------------------------------------- #
# 纯函数（与宿主解耦，便于单元测试）
# --------------------------------------------------------------------------- #

def parse_event_types(raw: Optional[str]) -> List[str]:
    """解析接受的事件名清单，逗号分隔，忽略空项与空白。"""
    if not raw:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def parse_mappings(raw: Optional[str]) -> List[Tuple[str, str]]:
    """
    解析路径映射配置。

    每行一条，支持两种分隔符：

    - ``媒体服务器路径#MoviePilot路径``（推荐，Windows 盘符路径也不会歧义）
    - ``媒体服务器路径:MoviePilot路径``（兼容历史配置）

    :param raw: 映射配置原文
    :return: [(媒体服务器路径, MoviePilot路径), ...]
    """
    mappings: List[Tuple[str, str]] = []
    if not raw:
        return mappings
    for line in str(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            left, right = line.split("#", 1)
        elif ":" in line:
            left, right = line.split(":", 1)
        else:
            continue
        left = left.strip().rstrip("/").rstrip("\\")
        right = right.strip().rstrip("/").rstrip("\\")
        if left and right:
            mappings.append((left, right))
    # 长前缀优先，避免 /a 抢在 /a/b 前面造成误替换
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    return mappings


def map_path(path: Optional[str], mappings: List[Tuple[str, str]]) -> str:
    """
    把媒体服务器路径按映射转换为 MoviePilot 路径。

    未命中任何映射时原样返回（此时由调用方按"路径不在映射范围内"处理）。

    :param path: 媒体服务器上报的路径
    :param mappings: 路径映射表
    :return: MoviePilot 侧路径
    """
    if not path:
        return ""
    normalized = str(path).replace("\\", "/")
    for server_root, local_root in mappings:
        if normalized == server_root or normalized.startswith(server_root + "/"):
            return local_root + normalized[len(server_root):]
    return normalized


def same_media(record_dest: Optional[str], event_path: Optional[str], is_tv: bool) -> bool:
    """
    判断整理记录与删除事件指向的是不是同一份媒体（**与文件后缀无关**）。

    规则：

    - 电影：父目录相同即认为是同一部（同一部片的多个版本 / 多分辨率都命中）；
      文件名主干相同也命中（跨目录整理的情况）。
    - 剧集：父目录是季目录，必须文件名主干相同，避免整季误删。

    :param record_dest: 整理记录的 dest
    :param event_path: 映射后的删除事件路径
    :param is_tv: 是否剧集
    :return: 是否同一份媒体
    """
    if not record_dest or not event_path:
        return False
    dest = Path(str(record_dest).replace("\\", "/"))
    target = Path(str(event_path).replace("\\", "/"))
    if is_tv:
        return dest.stem == target.stem
    if dest.stem == target.stem:
        return True
    # 同目录下的不同命名变体（整理记录里的文件名与媒体库文件名不完全一致时）。
    #
    # 这里刻意**不**做「事件路径是目录就直接命中该目录下记录」的匹配：
    # 实测生产日志 633 条 Movie 型删除事件全部是文件路径（0 条目录路径），
    # 而目录型路径恰恰来自整理换版重建（Emby 先报"文件夹被删"、随后影片又被加回来），
    # 一旦按目录命中就会把仍在库里的影片连源文件一起删掉，且不可逆。宁可漏清、不可误删。
    return dest.parent == target.parent


def is_within(path: Optional[str], roots: List[str]) -> bool:
    """
    判断路径是否位于给定根目录之一内部（用于删除前的最后一道护栏）。

    :param path: 待校验路径
    :param roots: 允许的根目录列表
    :return: 是否在允许范围内
    """
    if not path:
        return False
    target = os.path.abspath(str(path).replace("\\", "/"))
    for root in roots:
        if not root:
            continue
        root_abs = os.path.abspath(str(root).replace("\\", "/"))
        if target == root_abs or target.startswith(root_abs.rstrip("/") + "/"):
            return True
    return False


def is_media_file(path: Optional[str]) -> bool:
    """判断是否为媒体文件（按扩展名，空扩展名返回 False）。"""
    if not path:
        return False
    suffix = Path(str(path)).suffix.lower().lstrip(".")
    if not suffix:
        return False
    return suffix in {str(ext).lower().lstrip(".") for ext in settings.RMT_MEDIAEXT}


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #

class EmbySyncDel(_PluginBase):
    """根据媒体服务器删除事件同步清理整理记录、源文件、媒体库文件与下载任务。"""

    # 插件名称
    plugin_name = "Emby 联动删除"
    # 插件描述
    plugin_desc = "Emby 删除影片后同步清理整理记录、源文件、媒体库文件与下载任务；兼容 strm / 软链接媒体库。"
    # 插件图标（放在仓库 icons/ 目录，填文件名即可）
    plugin_icon = "embysyncdel.png"
    # 插件版本
    plugin_version = "1.3.0"
    # 插件作者
    plugin_author = "lutian98"
    # 作者主页
    author_url = "https://github.com/lutian98"
    # 插件配置项ID前缀
    plugin_config_prefix = "embysyncdel_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _dry_run: bool = True
    _event_types: str = DEFAULT_EVENT_TYPES
    _library_path: str = ""
    _exclude_path: str = ""
    _del_source: bool = True
    _del_seed: bool = True
    _torrent_action: str = ACTION_DELETE
    _title_guard: bool = True
    _del_history: bool = False

    _transferhis: Optional[TransferHistoryOper] = None
    _downloadhis: Optional[DownloadHistoryOper] = None
    _downloader_helper: Optional[DownloaderHelper] = None
    _default_downloader: Optional[str] = None

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """初始化插件：读取配置、准备宿主服务句柄。"""
        self._transferhis = TransferHistoryOper()
        self._downloadhis = DownloadHistoryOper()
        self._downloader_helper = DownloaderHelper()

        # 重置为默认值，避免卸载重装后残留旧配置
        self._enabled = False
        self._notify = False
        self._dry_run = True
        self._event_types = DEFAULT_EVENT_TYPES
        self._library_path = ""
        self._exclude_path = ""
        self._del_source = True
        self._del_seed = True
        self._torrent_action = ACTION_DELETE
        self._title_guard = True
        self._del_history = False

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify"))
            self._dry_run = bool(config.get("dry_run", True))
            self._event_types = str(config.get("event_types") or DEFAULT_EVENT_TYPES)
            self._library_path = str(config.get("library_path") or "")
            self._exclude_path = str(config.get("exclude_path") or "")
            self._del_source = bool(config.get("del_source", True))
            self._del_seed = bool(config.get("del_seed", True))
            self._torrent_action = str(config.get("torrent_action") or ACTION_DELETE)
            self._title_guard = bool(config.get("title_guard", True))
            self._del_history = bool(config.get("del_history"))
            # 直投 webhook 的密钥（Emby Webhooks 插件无法发送 MP 的 API Key，改用 URL 密钥自校验）
            self._webhook_key = str(config.get("webhook_key") or "").strip()
            if not self._webhook_key:
                self._webhook_key = uuid.uuid4().hex[:24]

            # 清理插件历史（一次性开关）
            if self._del_history:
                self.del_data(key="history")

            # 回写一次规范化配置：del_history 复位，避免每次加载都清空
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "dry_run": self._dry_run,
                "event_types": self._event_types,
                "library_path": self._library_path,
                "exclude_path": self._exclude_path,
                "del_source": self._del_source,
                "del_seed": self._del_seed,
                "torrent_action": self._torrent_action,
                "title_guard": self._title_guard,
                "del_history": False,
                "webhook_key": self._webhook_key,
            })

        # 默认下载器（缺少下载器信息时用于处理下载任务）
        self._default_downloader = None
        if self._downloader_helper:
            for name, service in (self._downloader_helper.get_services() or {}).items():
                if getattr(getattr(service, "config", None), "default", False):
                    self._default_downloader = name
                    break

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """退出插件（本插件无常驻服务）。"""
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不提供远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：接收媒体服务器直投的删除事件、查询历史、清空历史。"""
        return [
            {
                "path": "/emby_webhook",
                "endpoint": self.api_emby_webhook,
                "methods": ["POST"],
                # 媒体服务器的 Webhooks 插件无法发送 MP 的 API Key，
                # 因此放行宿主鉴权，由插件自己用 URL 里的 key 校验。
                "allow_anonymous": True,
                "auth": "apikey",
                "summary": "接收媒体服务器（Emby）删除事件 —— 供 Emby Webhooks 插件直接投递",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询联动删除历史",
            },
            {
                "path": "/delete_history",
                "endpoint": self.api_delete_history,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "清空联动删除历史",
            },
        ]

    def api_history(self) -> schemas.Response:
        """查询执行历史。"""
        return schemas.Response(success=True, data=self.get_data("history") or [])

    def api_delete_history(self) -> schemas.Response:
        """清空执行历史。"""
        self.save_data("history", [])
        return schemas.Response(success=True, message="已清空")

    # ------------------------------------------------------------------ #
    # 媒体服务器直投入口（Emby Webhooks 插件）—— 本插件能独立工作的关键
    # ------------------------------------------------------------------ #

    def api_emby_webhook(self, payload: Optional[dict] = None, key: str = "") -> dict:
        """接收媒体服务器直接投递的删除事件（原始 JSON）。

        为什么需要它：MoviePilot 的 Emby 解析器只认表单字段 ``data`` / 查询参数，
        **不读 JSON 请求体**（见 ``app/modules/emby/emby.py::get_webhook_message``），
        而 Emby 的 Webhooks 插件发的正是 JSON body → 事件被 MP 静默丢弃（HTTP 200、无日志），
        任何插件都收不到真实删除事件。本入口绕开那条解析链，直接吃原始 JSON：

            Emby → 插件 → Webhooks → Webhook URL 填
            ``http://<你的MP地址>/api/v1/plugin/EmbySyncDel/emby_webhook?key=<插件配置里的密钥>``

        处理放后台线程执行并立即返回，避免拖住 Emby 的 webhook 请求。
        """
        expected_key = str(getattr(self, "_webhook_key", "") or "")
        if not expected_key or str(key or "") != expected_key:
            self._record_event("api_rejected", "直投 webhook 密钥校验失败", "")
            return {"ok": False, "msg": "bad key"}
        if not isinstance(payload, dict) or not payload:
            self._record_event("api_rejected", "直投 webhook 报文体为空或非 JSON 对象", "")
            return {"ok": False, "msg": "bad body"}
        event_data = self._event_data_from_payload(payload)
        if not getattr(event_data, "event", ""):
            self._record_event("api_rejected", "直投 webhook 报文缺少 Event 字段", "")
            return {"ok": False, "msg": "no event"}
        # 标记来源，便于在历史里区分「媒体服务器直投」与「宿主事件」
        self._record_event(
            "api_received",
            f"收到媒体服务器直投事件：{event_data.event}",
            getattr(event_data, "item_name", ""),
            getattr(event_data, "media_id", "") or "",
        )

        def _run() -> None:
            try:
                self._handle_event_data(event_data)
            except Exception as err:  # noqa: BLE001
                logger.error(f"联动删除：直投 webhook 处理异常：{err}")
                self._record_event(
                    "error", f"直投 webhook 处理异常：{type(err).__name__}: {err}"
                )

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "msg": "accepted", "event": getattr(event_data, "event", "")}

    def _event_data_from_payload(self, payload: Dict[str, Any]) -> Any:
        """把媒体服务器原始报文整理成宿主事件对象（与 MP 解析器同形），复用同一条处理链。"""
        item = payload.get("Item") or payload.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        name = str(item.get("Name") or item.get("name") or "").strip()
        year = item.get("ProductionYear") or ""
        if name and year and str(year) not in name:
            name = f"{name} ({year})"
        media_source: Optional[MediaSource] = None
        media_id: Optional[str] = None
        provider_ids = item.get("ProviderIds") or {}
        if isinstance(provider_ids, dict) and provider_ids:
            try:
                media_source, media_id = MediaServerIdentityHelper.from_provider_ids(provider_ids)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"联动删除：报文 ProviderIds 解析失败：{err}")
        item_type = str(item.get("Type") or item.get("type") or "")
        return SimpleNamespace(
            event=str(payload.get("Event") or payload.get("event") or ""),
            channel=str(payload.get("channel") or payload.get("Channel") or "emby"),
            item_type=item_type,
            media_type=item_type,
            item_name=name,
            item_path=str(item.get("Path") or item.get("path") or ""),
            media_source=media_source,
            media_id=str(media_id) if media_id else None,
            # 原始报文：身份兜底（ProviderIds）等逻辑从这里取
            json_object=payload,
        )

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面：1、页面配置；2、数据结构。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "演练模式（只出清单不删除）",
                                            "hint": "建议首次启用时保持开启：插件只发通知列出将要删除的内容，不做任何删除。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "del_history",
                                            "label": "清理插件历史",
                                            "hint": "打开后保存一次配置即清空历史记录。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "event_types",
                                            "rows": 2,
                                            "label": "接受的删除事件名",
                                            "placeholder": DEFAULT_EVENT_TYPES,
                                            "hint": "逗号分隔；不确定媒体服务器发哪个事件名时，把三种都留着即可。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "library_path",
                                            "rows": 3,
                                            "label": "媒体库路径映射",
                                            "placeholder": "媒体服务器路径#MoviePilot路径（一行一个）",
                                            "hint": "例如：/mnt/user/Media/Strm/Media#/downloads/link",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_path",
                                            "rows": 2,
                                            "label": "排除路径（不处理）",
                                            "placeholder": "一行一个；命中前缀的媒体路径直接跳过",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "del_source", "label": "删除源文件与传输文件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "del_seed",
                                            "label": "同时处理辅种",
                                            "hint": "递归清理由转种/辅种插件记录的关联任务。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "title_guard",
                                            "label": "标题防误删校验",
                                            "hint": "要求整理记录标题出现在删除媒体名中，避免误删同名不同片。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "torrent_action",
                                            "label": "下载任务处理方式",
                                            "items": [
                                                {"title": "删除任务与文件", "value": ACTION_DELETE},
                                                {"title": "暂停任务（保留文件）", "value": ACTION_STOP},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webhook_key",
                                            "label": "直投 Webhook 密钥",
                                            "hint": "媒体服务器直投入口的校验密钥，首次启用自动生成，可自行更换。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": "直投入口（推荐）：媒体服务器 Webhooks 插件的地址填 "
                                    "http://你的MP地址/api/v1/plugin/EmbySyncDel/emby_webhook?key=上面的密钥 —— "
                                    "MoviePilot 自带的 Emby 解析器不读 JSON 请求体，Emby 的删除事件会被它静默丢弃，"
                                    "走这个直投入口才收得到。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": "删除动作不可逆：媒体库文件、源文件与下载任务会被一并清理。建议先用演练模式跑一遍，核对清单无误后再关闭演练模式。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": "路径映射务必填写「媒体服务器看到的媒体库根目录」→「MoviePilot 容器内的媒体库根目录」。strm / 软链接媒体库无需规避后缀差异，插件按目录与文件名主干匹配。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dry_run": True,
            "del_history": False,
            "event_types": DEFAULT_EVENT_TYPES,
            "library_path": "",
            "exclude_path": "",
            "del_source": True,
            "del_seed": True,
            "title_guard": True,
            "torrent_action": ACTION_DELETE,
            "webhook_key": "",
        }

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del_by_webhook(self, event: Event) -> None:
        """媒体服务器删除事件 → 同步清理（外层兜底：异常也留痕）。"""
        try:
            self._handle_webhook_event(event)
        except Exception as err:  # noqa: BLE001 - 兜底留痕，避免事件处理异常静默丢失
            logger.error(f"联动删除：处理 webhook 事件异常：{err}")
            self._record_event(
                "error", f"处理事件异常：{type(err).__name__}: {err}"
            )

    def _handle_webhook_event(self, event: Event) -> None:
        """媒体服务器删除事件 → 同步清理。"""
        if not self._enabled or not event:
            return
        self._handle_event_data(event.event_data)

    def _handle_event_data(self, event_data: Any) -> None:
        """事件体处理链：宿主 webhook 事件与「媒体服务器直投 JSON」共用同一条逻辑。"""
        if not self._enabled or not event_data:
            return

        event_type = str(event_data.event or "")
        allowed = parse_event_types(self._event_types)
        media_name = str(event_data.item_name or "").strip()
        media_path = str(event_data.item_path or "").replace("\\", "/")
        media_type = str(event_data.media_type or event_data.item_type or "")
        self._record_event(
            "received",
            f"事件={event_type or '(空)'}；媒体类型={media_type or '(空)'}；名称={media_name or '(空)'}；"
            f"路径={media_path or '(空)'}",
            media_name=media_name,
        )
        if not event_type or event_type not in allowed:
            self._record_event(
                "ignored",
                f"事件名不在监听列表（收到 {event_type or '(空)'}，监听 {self._event_types}）",
                media_name,
            )
            return

        if not media_path:
            logger.warning(f"联动删除：事件缺少媒体路径，已忽略（{media_name}）")
            self._record_event("ignored", "事件缺少媒体路径", media_name)
            return

        # 容器级事件（文件夹 / 剧集容器）默认不处理。
        #
        # 实测（2026-10-08，Emby 生产环境）：
        # - library.deleted 里 Folder 型占一半（614 条），来源是「整理换版重建」和其它媒体库的清理动作；
        #   典型样本：08:42:26 入库 → 09:02:59 报「文件夹被删」→ 09:03:19 影片又被加回来 —— 影片其实一直在库里。
        # - 这类事件没有影片文件名，靠目录名匹配整理记录极易把**仍在库里的影片**连源文件一起删掉，且不可逆。
        # 因此默认拒绝，只留痕；确实需要处理这类事件的（例如整目录级删除）再显式打开开关。
        container_type = media_type.strip().lower()
        if container_type in CONTAINER_ITEM_TYPES:
            logger.info(
                f"联动删除：容器级事件（{media_type}）默认不处理，已跳过：{media_path}"
            )
            self._record_event(
                "skipped_container",
                f"容器级事件（{media_type}）默认不处理：{media_path}"
                f"（该事件多为整理重建或其它库清理，据目录名匹配会误删仍在库里的影片，故一律不处理）",
                media_name,
            )
            return

        # 排除路径
        if self._excluded(media_path):
            logger.info(f"联动删除：媒体路径 {media_path} 命中排除规则，已跳过")
            self._record_event("ignored", f"路径命中排除规则：{media_path}", media_name)
            return

        # 媒体身份：事件自带 → 报文提供方 ID 兜底
        media_source, media_id = resolve_media_identity(event_data)
        if not media_source or not media_id:
            provider_ids = self._provider_ids(event_data)
            if provider_ids:
                media_source, media_id = MediaServerIdentityHelper.from_provider_ids(provider_ids)
        if not media_source or not media_id:
            # 2026-10-07：真实媒体服务器（Emby）的删除报文常常不带 ProviderIds，
            # 此时**不再直接放弃**：只要有可映射的路径，就按「路径 + 标题守卫」匹配整理记录；
            # 仍然要求匹配唯一，不唯一照样放弃并告警。
            logger.info(
                f"联动删除：{media_name} 事件未带媒体身份，改用媒体路径匹配：{media_path}"
            )
            self._record_event(
                "no_identity",
                f"事件未带媒体身份（缺来源/原生 ID），改用媒体路径匹配：{media_path}",
                media_name,
            )

        self._sync_del(
            media_name=media_name,
            media_path=media_path,
            media_type=media_type,
            media_source=media_source,
            media_id=str(media_id),
        )

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def _sync_del(
            self,
            media_name: str,
            media_path: str,
            media_type: str,
            media_source: MediaSource,
            media_id: str,
    ) -> None:
        """查找整理记录并执行清理。"""
        is_tv = media_type not in ("Movie", "MOV", "Video", "")
        target_path = map_path(media_path, parse_mappings(self._library_path))
        if not self._library_path:
            logger.warning("联动删除：未配置媒体库路径映射，将直接使用媒体服务器路径匹配")

        records = self._find_records(
            media_name=media_name,
            target_path=target_path,
            media_source=media_source,
            media_id=media_id,
            media_type=media_type,
        )
        if not records:
            message = (
                f"联动删除未找到匹配的整理记录：{media_name or media_id}\n"
                f"媒体服务器路径：{media_path}\n"
                f"匹配用路径：{target_path}\n"
                f"请检查媒体库路径映射是否正确（媒体服务器路径#MoviePilot路径）。"
            )
            logger.warning(message)
            self._record_event("no_record", message, media_name, media_id)
            self._notify_result(title="⚠️ 联动删除未找到整理记录", text=message,
                                image=self._fallback_image())
            return

        plan: List[Dict[str, Any]] = []
        results: List[str] = []
        error_cnt = 0

        if self._dry_run:
            for record in records:
                plan.append({
                    "id": record.id,
                    "title": record.title,
                    "dest": record.dest,
                    "src": record.src,
                    "hash": record.download_hash,
                })
            text = "\n".join(
                f"• {item['title']}（记录 {item['id']}）\n   库文件：{item['dest']}\n   源文件：{item['src']}"
                for item in plan
            )
            if media_path and Path(media_path).suffix.lower() == ".strm":
                text += f"\n\n媒体服务器占位文件：{media_path}"
            self._notify_result(
                title=f"🧪 演练：{media_name} 将删除 {len(plan)} 项",
                text=f"{text}\n\n当前为演练模式，未执行任何删除。核对无误后请在插件配置中关闭「演练模式」。",
                image=self._poster_image(records),
                mtype=MessageType.Plugin,
            )
            logger.info(f"联动删除（演练）：{media_name} 命中 {len(plan)} 条整理记录")
            self._append_history(
                media_name,
                media_id,
                [f"🧪 演练：将删除 {len(plan)} 项（未执行任何删除）"]
                + [
                    f"• {item['title']}（记录 {item['id']}）库文件：{item['dest']}；源文件：{item['src']}"
                    for item in plan
                ],
                0,
            )
            return

        for record in records:
            if self._title_guard and record.title and record.title not in media_name:
                logger.warning(
                    f"联动删除：整理记录 {record.id}（{record.title}）与删除媒体 {media_name} 不符，"
                    f"防误删已跳过"
                )
                continue
            try:
                results.extend(self._delete_record(record))
            except Exception as err:  # noqa: BLE001 - 单条失败不影响其余记录
                error_cnt += 1
                results.append(f"• 记录 {record.id} 处理失败：{err}")
                logger.error(f"联动删除：整理记录 {record.id} 处理失败：{err}")

        # 占位文件（strm）一并清理：只删占位、不碰真实媒体文件，避免媒体服务器下次扫描又把条目加回来
        if self._del_source:
            results.extend(self._remove_placeholder(media_path))

        self._append_history(media_name, media_id, results, error_cnt)
        if self._notify:
            head = "✅" if not error_cnt else "⚠️"
            body = "\n".join(results) if results else "无可删除内容"
            self._notify_result(
                title=f"{head} 联动删除完成：{media_name}",
                text=f"{body}\n\n成功处理 {len(records) - error_cnt} 条，失败 {error_cnt} 条。",
                image=self._poster_image(records),
                mtype=MessageType.Plugin,
            )

    def _delete_record(self, record: Any) -> List[str]:
        """删除单条整理记录对应的文件与下载任务，最后删除记录本身。"""
        lines: List[str] = []
        allowed_roots = self._allowed_roots()
        # 整理记录是清理失败后的重试依据：外部清理没做干净就保留记录，下次还能补删
        cleanup_ok = True

        # 1、媒体库文件与源文件
        if self._del_source:
            if record.dest:
                if is_within(record.dest, allowed_roots):
                    lines.extend(self._remove_file(record.dest, "媒体库文件"))
                else:
                    cleanup_ok = False
                    lines.append(f"• 跳过媒体库文件（不在映射范围内，请检查路径映射）：{record.dest}")
                    logger.warning(
                        f"联动删除：媒体库文件 {record.dest} 不在映射范围内，已跳过；"
                        f"请检查「媒体库路径映射」配置"
                    )
            if record.src:
                if is_media_file(record.src):
                    lines.extend(self._remove_file(record.src, "源文件"))
                else:
                    lines.append(f"• 跳过源文件（非媒体文件）：{record.src}")
        # 2、下载任务（含转种与辅种）
        lines.extend(self._handle_torrents(record))

        # 3、整理记录最后删除：它是跨文件系统 / 下载器清理失败后的重试依据
        if cleanup_ok and self._transferhis:
            self._transferhis.delete(record.id)
            lines.append(f"• 已删除整理记录：{record.id}（{record.title}）")
        else:
            lines.append(f"• 外部清理未完成，保留整理记录以便重试：{record.id}")
        return lines

    def _remove_file(self, path: str, label: str) -> List[str]:
        """删除文件并清理空的父目录。"""
        lines: List[str] = []
        target = Path(str(path).replace("\\", "/"))
        try:
            if target.exists():
                target.unlink(missing_ok=True)
                lines.append(f"• 已删除{label}：{path}")
                self._prune_empty_dirs(target.parent)
            else:
                lines.append(f"• {label}不存在，已跳过：{path}")
        except Exception as err:  # noqa: BLE001
            lines.append(f"• 删除{label}失败：{path}（{err}）")
            logger.error(f"联动删除：删除{label} {path} 失败：{err}")
        return lines

    def _remove_placeholder(self, media_path: str) -> List[str]:
        """
        清理媒体服务器侧的占位文件（strm）。

        只处理 .strm 占位文件：真实媒体文件由整理记录里的路径负责删除，两者互不重复。
        占位文件不清理时，媒体服务器下次扫描会把条目重新加回媒体库。
        """
        if not media_path or Path(str(media_path)).suffix.lower() != ".strm":
            return []
        server_roots = [server for server, _ in parse_mappings(self._library_path)]
        if server_roots and not is_within(media_path, server_roots):
            return [f"• 跳过占位文件（不在媒体服务器映射范围内）：{media_path}"]
        target = Path(str(media_path).replace("\\", "/"))
        if not target.exists():
            return [f"• 占位文件不存在或未挂载到 MoviePilot，已跳过：{media_path}"]
        return self._remove_file(media_path, "占位文件(strm)")

    def _prune_empty_dirs(self, directory: Path) -> None:
        """自底向上删除空目录，遇到非空目录或受保护目录即停止。"""
        protected = {Path(os.path.abspath(root)) for root in self._allowed_roots()}
        current = Path(os.path.abspath(str(directory)))
        for _ in range(8):
            if current in protected or not current.is_dir():
                return
            try:
                next(current.iterdir())
                return
            except StopIteration:
                pass
            except Exception:  # noqa: BLE001
                return
            try:
                current.rmdir()
                logger.info(f"联动删除：已删除空目录 {current}")
                current = current.parent
            except Exception:  # noqa: BLE001
                return

    # ------------------------------------------------------------------ #
    # 记录查找
    # ------------------------------------------------------------------ #

    def _find_records(
            self,
            media_name: str,
            target_path: str,
            media_source: MediaSource,
            media_id: str,
            media_type: str,
    ) -> List[Any]:
        """
        查找与删除事件匹配的整理记录。

        顺序：身份精确查询 → 目录 / 文件名主干兜底；无论走哪条路径，最终都要求
        记录与事件指向同一份媒体（见 :func:`same_media`），因此后缀差异不影响匹配。
        """
        is_tv = media_type not in ("Movie", "MOV", "Video", "")
        candidates: List[Any] = []

        # ① 按媒体身份查询
        try:
            records, _ = self._transferhis.query(
                TransferHistoryFilter(media_source=media_source, media_id=str(media_id)),
                QueryPageRequest(page=1, count=RECORD_QUERY_PAGE_SIZE),
            )
            candidates.extend(records or [])
        except Exception as err:  # noqa: BLE001 - 契约变化时退回文本查询
            logger.warning(f"联动删除：按媒体身份查询整理记录失败，改用文本匹配：{err}")
            try:
                records, _ = self._transferhis.query(
                    TransferHistoryFilter(text=str(media_id)),
                    QueryPageRequest(page=1, count=RECORD_QUERY_PAGE_SIZE),
                )
                candidates.extend(records or [])
            except Exception as inner:  # noqa: BLE001
                logger.error(f"联动删除：整理记录查询失败：{inner}")

        # ② 目录 / 文件名主干匹配（strm 兼容的关键）
        matched = [r for r in candidates if same_media(r.dest, target_path, is_tv)]

        # ③ 仍未命中时，用映射后的目录名做文本包含查询兜底
        if not matched:
            keywords = [keyword for keyword in {
                Path(target_path).stem if target_path else "",
                Path(target_path).parent.name if target_path else "",
                media_name,
            } if keyword]
            for keyword in keywords:
                try:
                    records, _ = self._transferhis.query(
                        TransferHistoryFilter(text=keyword),
                        QueryPageRequest(page=1, count=RECORD_QUERY_PAGE_SIZE),
                    )
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"联动删除：文本查询 {keyword} 失败：{err}")
                    continue
                for record in records or []:
                    if record in candidates:
                        continue
                    candidates.append(record)
                    if same_media(record.dest, target_path, is_tv):
                        matched.append(record)
                if matched:
                    break

        if not matched:
            logger.warning(
                f"联动删除：媒体身份 {media_source}:{media_id} 命中 {len(candidates)} 条整理记录，"
                f"但没有一条能与 {target_path} 对应上"
            )
        else:
            logger.info(f"联动删除：命中 {len(matched)} 条整理记录（{media_name}）")
        return matched

    # ------------------------------------------------------------------ #
    # 下载任务
    # ------------------------------------------------------------------ #

    def _handle_torrents(self, record: Any) -> List[str]:
        """处理整理记录关联的下载任务：主任务 + 转种 + 辅种。"""
        lines: List[str] = []
        torrent_hash = getattr(record, "download_hash", None)
        if not torrent_hash:
            return lines
        downloader = self._downloader_of(torrent_hash) or self._default_downloader
        lines.extend(self._handle_one_torrent(str(torrent_hash), downloader, self._torrent_action))

        if self._del_seed:
            lines.extend(self._handle_seeds(str(torrent_hash), set()))
        return lines

    def _downloader_of(self, torrent_hash: str) -> Optional[str]:
        """通过下载历史反查种子所属下载器。"""
        if not self._downloadhis:
            return None
        try:
            files = self._downloadhis.get_files_by_hash(download_hash=torrent_hash)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"联动删除：查询种子 {torrent_hash} 的文件记录失败：{err}")
            return None
        for item in files or []:
            downloader = getattr(item, "downloader", None)
            if downloader:
                return str(downloader)
        return None

    def _handle_one_torrent(
            self, torrent_hash: str, downloader: Optional[str], action: str
    ) -> List[str]:
        """按配置删除或暂停一个下载任务，并同步处理转种记录。"""
        lines: List[str] = []
        if not torrent_hash:
            return lines

        # 转种记录：源种与目标种都要处理
        transfer = None
        if downloader:
            try:
                transfer = self.get_data(key=f"{downloader}-{torrent_hash}", plugin_id="TorrentTransfer")
            except Exception:  # noqa: BLE001 - 未安装转种插件属正常情况
                transfer = None
        if transfer and isinstance(transfer, dict):
            target_downloader = transfer.get("to_download")
            target_hash = transfer.get("to_download_id")
            if target_hash:
                lines.extend(self._act_torrent(str(target_hash), target_downloader, action, "转种任务"))
            source_downloader = transfer.get("from_download")
            source_hash = transfer.get("from_download_id")
            if source_hash and source_hash != torrent_hash:
                lines.extend(self._act_torrent(str(source_hash), source_downloader, action, "源下载任务"))

        lines.extend(self._act_torrent(torrent_hash, downloader, action))
        return lines

    def _act_torrent(
            self,
            torrent_hash: str,
            downloader: Optional[str],
            action: str,
            label: str = "下载任务",
    ) -> List[str]:
        """对下载器执行删除 / 暂停。"""
        if not torrent_hash or not self.chain:
            return []
        try:
            if action == ACTION_STOP:
                self.chain.stop_torrents(hashs=torrent_hash, downloader=downloader)
                return [f"• 已暂停{label}：{torrent_hash}"]
            self.chain.remove_torrents(hashs=torrent_hash, delete_file=True, downloader=downloader)
            return [f"• 已删除{label}：{torrent_hash}"]
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：处理{label} {torrent_hash} 失败：{err}")
            return [f"• 处理{label}失败：{torrent_hash}（{err}）"]

    def _handle_seeds(self, torrent_hash: str, visited: set, depth: int = 0) -> List[str]:
        """递归处理辅种记录（读取辅种插件保存的关联任务）。"""
        lines: List[str] = []
        if depth >= MAX_SEED_DEPTH or not torrent_hash or torrent_hash in visited:
            return lines
        visited.add(torrent_hash)
        try:
            seed_history = self.get_data(key=torrent_hash, plugin_id="IYUUAutoSeed") or []
        except Exception:  # noqa: BLE001 - 未安装辅种插件属正常情况
            return lines
        if not isinstance(seed_history, list):
            return lines
        for item in seed_history:
            if not isinstance(item, dict):
                continue
            downloader = item.get("downloader")
            torrents = item.get("torrents")
            if isinstance(torrents, str):
                torrents = [torrents]
            for seed_hash in torrents or []:
                lines.extend(self._act_torrent(str(seed_hash), downloader, self._torrent_action, "辅种任务"))
                lines.extend(self._handle_seeds(str(seed_hash), visited, depth + 1))
        return lines

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def _allowed_roots(self) -> List[str]:
        """允许删除的根目录：媒体库映射目标 + 源文件映射目标。"""
        roots = [local for _, local in parse_mappings(self._library_path)]
        return roots

    def _excluded(self, media_path: str) -> bool:
        """判断媒体路径是否命中排除规则。"""
        if not self._exclude_path:
            return False
        for line in str(self._exclude_path).splitlines():
            keyword = line.strip()
            if keyword and keyword in media_path:
                return True
        return False

    @staticmethod
    def _provider_ids(event_data: Any) -> Dict[str, Any]:
        """从事件原始报文中取出提供方 ID（ProviderIds）。"""
        try:
            payload = event_data.json_object or {}
            item = payload.get("Item") or {}
            provider_ids = item.get("ProviderIds") or {}
            return provider_ids if isinstance(provider_ids, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _fallback_image() -> str:
        """默认通知图片（整理记录里没有海报时用）。"""
        return "https://raw.githubusercontent.com/lutian98/MoviePilot-Plugins/main/icons/embysyncdel.png"

    @staticmethod
    def _poster_image(records: Any) -> str:
        """取通知配图：优先用整理记录自带的海报，其次兜底图标。

        整理记录（transferhistory）自带 image 字段，MP 在整理入库时就把海报存进去了，
        比再去 TMDB 查一次更稳、更省事。
        """
        for record in records or []:
            image = getattr(record, "image", None)
            if image and isinstance(image, str) and image.startswith(("http://", "https://")):
                return image
        return EmbySyncDel._fallback_image()

    def _record_event(
            self, stage: str, detail: str, media_name: str = "", media_id: str = ""
    ) -> None:
        """记录一次联动删除事件的阶段结果（审计用）。

        任何一个阶段都会留痕，便于排查「事件是否到达 / 为何跳过」。
        """
        try:
            history = self.get_data("history") or []
            if not isinstance(history, list):
                history = []
            history.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "stage": stage,
                "name": media_name,
                "media_id": media_id,
                "detail": [detail] if detail else [],
                "error": 0,
            })
            self.save_data("history", history[-MAX_HISTORY:])
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：写入审计记录失败：{err}")

    def _append_history(
            self, media_name: str, media_id: str, results: List[str], error_cnt: int
    ) -> None:
        """写入执行历史（最多保留 MAX_HISTORY 条）。"""
        try:
            history = self.get_data("history") or []
            if not isinstance(history, list):
                history = []
            history.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "stage": "done",
                "name": media_name,
                "media_id": media_id,
                "detail": results,
                "error": error_cnt,
            })
            self.save_data("history", history[-MAX_HISTORY:])
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：写入历史失败：{err}")

    def _notify_result(
            self,
            title: str,
            text: str,
            image: Optional[str] = None,
            mtype: MessageType = MessageType.Plugin,
    ) -> None:
        """发送通知（演练 / 未匹配 / 正常完成 都会发）。"""
        if not self._notify:
            return
        try:
            self.post_message(mtype=mtype, title=title, text=text, image=image)
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：发送通知失败：{err}")

    def get_page(self) -> Optional[List[dict]]:
        """插件详情页：展示最近的联动删除历史。"""
        history = self.get_data("history") or []
        if not isinstance(history, list) or not history:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无联动删除记录。",
                    },
                }
            ]
        items = []
        for item in reversed(history[-20:]):
            detail = "；".join(item.get("detail") or []) or "无"
            error_cnt = int(item.get("error") or 0)
            items.append({
                "component": "VExpansionPanel",
                "props": {
                    "title": f"{item.get('time')}　{item.get('name')}"
                             + ("　⚠️ 有失败项" if error_cnt else ""),
                },
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": detail,
                    }
                ],
            })
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "最近联动删除记录"},
                    {"component": "VDivider"},
                    {"component": "VCardText", "content": [{"component": "VExpansionPanels", "content": items}]},
                ],
            }
        ]
