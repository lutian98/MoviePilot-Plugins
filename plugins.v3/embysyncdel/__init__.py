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
3. **媒体身份三级兜底** —— 插件事件 → 提供方 ID（ProviderIds）→ **媒体路径匹配**
   （要求路径对应、命中记录唯一，并叠加标题防误删校验），任一层拿到身份即可继续，
   避免"身份缺失即整体失效"。
4. **默认只出清单** —— ``dry_run`` 默认开启：只发通知列出将要删除的内容，不动任何数据。

License: GPL-3.0
"""

import hmac
import os
import re
import shutil
import time
import unicodedata
import threading
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

# 界面上给用户勾选的候选事件名（下拉多选）。带中文说明，避免用户对着字符串猜。
KNOWN_EVENT_TYPES: List[Tuple[str, str]] = [
    ("library.deleted", "library.deleted —— Emby 新版默认（最常见）"),
    ("ItemDeleted", "ItemDeleted —— Emby 旧版 / Webhooks 插件老版本"),
    ("item.deleted", "item.deleted —— 部分 Emby / Jellyfin 版本"),
    ("library.removed", "library.removed —— Jellyfin 风格"),
    ("media.deleted", "media.deleted —— 第三方媒体服务器 / 自定义"),
    ("media_del", "media_del —— Scripter X 等脚本工具"),
]
# 自定义补充项（勾选列表里没有的冷门事件名写这里）
DEFAULT_EVENT_TYPES_EXTRA = ""

# 容器级条目类型：这些事件指的是「文件夹 / 剧集容器」，不是具体影片文件。
# 实测这类事件大量来自整理换版重建与其它媒体库的清理动作，按目录名匹配极易误删仍在库里的影片。
# 电影类媒体类型（与 CONTAINER_ITEM_TYPES 并列的单一事实源）。
# 注意：**空字符串不在此列** —— 类型缺失时按剧集口径做严格匹配，避免把未知类型当电影走"父目录即命中"。
MOVIE_ITEM_TYPES = ("Movie", "MOV", "Video")

CONTAINER_ITEM_TYPES = {
    "folder", "series", "season", "collectionfolder", "boxset", "collection",
}
# 下载任务处理方式
ACTION_DELETE = "delete"
ACTION_STOP = "stop"
# 辅种递归处理的最大深度，防止环路
MAX_SEED_DEPTH = 5
# CrossSeed（青蛙辅种助手）**不落库**，靠给下载器里的种子打标签记录辅种关系；
# 这些辅种与主体是同一份文件，删主体后它们会变成死种，需要一并清掉。
DEFAULT_SEED_TAGS = "辅种"
# 插件自带历史最多保留条数
MAX_HISTORY = 200
# 插件历史页展示条数
HISTORY_PAGE_SIZE = 20
# 直投 webhook 自动生成的密钥长度
# 删除空目录时最多向上回退的层数（防止沿父目录一路删到媒体库之外）
MAX_PRUNE_DEPTH = 8
# 同一删除事件在这么长时间内只处理一次（Emby 重投 / 代理重放 / 双入口同时到达）
EVENT_DEDUP_WINDOW = 60
# 整理记录的查询上限（同一部片的多版本记录通常只有几条）
RECORD_QUERY_PAGE_SIZE = 200


# --------------------------------------------------------------------------- #
# 纯函数（与宿主解耦，便于单元测试）
# --------------------------------------------------------------------------- #

def parse_event_types(raw: Any) -> List[str]:
    """解析接受的事件名清单。

    兼容三种存储形态：下拉多选产生的 list、历史配置的逗号分隔字符串、以及混合内容 ——
    这样老配置不用迁移，用户也能在界面上直接勾选。
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = str(raw).replace("\n", ",").split(",")
    return [item.strip() for item in items if str(item).strip()]


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
        # 统一反斜杠：map_path 与 is_within 对同一份映射必须用同一套路径口径
        left = left.strip().replace("\\", "/").rstrip("/")
        right = right.strip().replace("\\", "/").rstrip("/")
        if left and right:
            mappings.append((left, right))
    # 长前缀优先，避免 /a 抢在 /a/b 前面造成误替换
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    return mappings


_MAPPINGS_CACHE: Dict[str, List[Tuple[str, str]]] = {}


def cached_mappings(raw: Optional[str]) -> List[Tuple[str, str]]:
    """带缓存的 parse_mappings：同一个配置串在一次删除流程里会被解析十几次，结果必然相同。"""
    key = str(raw or "")
    cached = _MAPPINGS_CACHE.get(key)
    if cached is None:
        cached = parse_mappings(raw)
        if len(_MAPPINGS_CACHE) > 32:      # 配置只会有寥寥几份，防止有人疯狂改配置把内存撑大
            _MAPPINGS_CACHE.clear()
        _MAPPINGS_CACHE[key] = cached
    return cached


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


def normalize_title(text: Optional[str]) -> str:
    """
    标题归一化：用于「整理记录标题 vs 媒体名」的防误删比较。

    只做与"同一部片的不同写法"有关的归一（全角半角、大小写、年份、分辨率与来源后缀、空白与常见标点），
    **保留冒号** —— 冒号是副标题分隔符，「X」与「X：副标题」是两部不同的片，归一后必须仍然不等。
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).lower()
    value = re.sub(r"\.(mkv|mp4|avi|ts|strm|iso|m2ts|rmvb|mov|wmv|flv|ass|srt)$", "", value)
    value = re.sub(r"\b(19|20)\d{2}\b", " ", value)
    value = re.sub(r"\b(1080|720|2160|480|576)[pi]\b", " ", value)
    value = re.sub(
        r"\b(bluray|blu-ray|web-?dl|webrip|hdrip|dvdrip|hdtv|remux|uhd|x26[45]|h26[45]|hevc|avc|aac|ac3|eac3|dts|"
        r"truehd|atmos|10bit|8bit|hdr|sdr|default|chi|zh-cn|gb|big5)\b", " ", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff:]+", "", value)
    return value


def title_matches(record_title: Optional[str], media_name: Optional[str]) -> bool:
    """
    防误删判据：整理记录标题必须与媒体名**归一化后完全相等**。

    为什么不用子串包含（原实现 ``record.title in media_name``）：
    整理记录标题经常是媒体名的真前缀（「X」与「X：副标题」「X2」是不同影片），用 ``in`` 会把
    续集/副标题片误判为同一部；而且记录标题为空（未刮削）时 ``in`` 判断整条失效，护栏形同不存在。
    """
    left = normalize_title(record_title)
    right = normalize_title(media_name)
    if not left or not right:
        return False
    return left == right


def same_media(
        record_dest: Optional[str],
        event_path: Optional[str],
        is_tv: bool,
        record_title: Optional[str] = None,
        media_name: Optional[str] = None,
) -> bool:
    """
    判断整理记录与删除事件指向的是不是同一份媒体（**与文件后缀无关**）。

    规则（宁可漏清、不可误删）：

    - 非电影（含未知/空类型）：**必须文件名主干完全相同**（剧集季目录下用于避免整季误删）。
    - 电影：文件名主干相同即命中（跨目录整理、strm 与真实文件同名主干都覆盖）；
      仅"父目录相同"还不够，必须**再叠加标题一致**（record_title 与 media_name 归一化后相等），
      否则同一目录下放着的另一部片（合集/扁平媒体库）会被连带删除。

    :param record_dest: 整理记录的 dest
    :param event_path: 映射后的删除事件路径
    :param is_tv: 是否剧集/未知类型
    :param record_title: 整理记录的标题（同目录匹配时的二次确认）
    :param media_name: 事件上报的媒体名
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
    #
    # 同目录命中还必须叠加"标题一致"这道二次确认：同一目录下可能放着不同的影片
    #（合集打包、扁平媒体库、正片与特典混放），只按父目录相等会把它们一起删掉。
    if dest.parent == target.parent and title_matches(record_title, media_name):
        return True
    return False


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
        if root_abs in ("/", ""):
            # 根写成 "/" 时前缀比较会退化成"全盘放行"，直接忽略这条无效根
            continue
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


# 附属文件扩展名白名单：字幕 / 外挂音轨 / 元数据与图片。
# 这类文件与片子同生共死，源目录清理时必须一并删掉，否则会残留成
# 「外部清理未完成」，整理记录也删不掉（2026-10-13 用户口径：一并清）。
AUX_EXTS = {
    "ass", "ssa", "srt", "sup", "sub", "idx", "vtt", "smi", "sami", "ttml",
    "mka", "flac", "aac", "ac3", "dts", "m4a", "ape", "wav",
    "nfo", "txt", "url", "sfv", "md5", "sha1", "cue", "log", "tmm",
    "jpg", "jpeg", "png", "webp", "bmp", "gif",
}


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
    plugin_version = "1.5.4"
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
        self._event_types_extra = DEFAULT_EVENT_TYPES_EXTRA
        self._library_path = ""
        self._exclude_path = ""
        self._source_roots = ""
        self._del_source = True
        self._del_seed = True
        self._seed_tags = DEFAULT_SEED_TAGS
        self._torrent_action = ACTION_DELETE
        self._title_guard = True
        self._del_history = False

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify"))
            self._dry_run = bool(config.get("dry_run", True))
            raw_events = config.get("event_types")
            parsed_events = parse_event_types(raw_events)
            if parsed_events:
                # 界面多选存的是 list，老配置存的是逗号分隔字符串 —— 统一成字符串存储，兼容两边
                self._event_types = ",".join(parsed_events) if isinstance(raw_events, str) else parsed_events
            else:
                self._event_types = DEFAULT_EVENT_TYPES
            self._event_types_extra = str(config.get("event_types_extra") or "")
            self._library_path = str(config.get("library_path") or "")
            self._exclude_path = str(config.get("exclude_path") or "")
            self._source_roots = str(config.get("source_roots") or "")
            self._del_source = bool(config.get("del_source", True))
            self._del_seed = bool(config.get("del_seed", True))
            self._seed_tags = str(config.get("seed_tags") or DEFAULT_SEED_TAGS)
            self._torrent_action = str(config.get("torrent_action") or ACTION_DELETE)
            self._title_guard = bool(config.get("title_guard", True))
            self._del_history = bool(config.get("del_history"))
            # 直投 webhook 的密钥（Emby Webhooks 插件无法发送 MP 的 API Key，改用 URL 密钥自校验）

            # 清理插件历史（一次性开关）
            if self._del_history:
                self.del_data(key="history")

            # 回写一次规范化配置：del_history 复位，避免每次加载都清空
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "dry_run": self._dry_run,
                "event_types": self._event_types,
                "event_types_extra": self._event_types_extra,
                "library_path": self._library_path,
                "exclude_path": self._exclude_path,
                "del_source": self._del_source,
                "del_seed": self._del_seed,
                "seed_tags": self._seed_tags,
                "torrent_action": self._torrent_action,
                "title_guard": self._title_guard,
                "del_history": False,
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

    def api_history(self) -> schemas.Response:
        """查询执行历史。"""
        return schemas.Response(success=True, data=self.get_data("history") or [])

    def api_delete_history(self) -> schemas.Response:
        """清空执行历史。"""
        self.save_data("history", [])
        return schemas.Response(success=True, message="已清空")

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #

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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "event_types",
                                            "label": "接受哪些删除事件",
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "items": [{"title": desc, "value": value}
                                                      for value, desc in KNOWN_EVENT_TYPES],
                                            "hint": "勾选媒体服务器会发出来的删除事件名；不确定就三个都勾着。"
                                                    "列表里没有的冷门事件名写在下面「自定义」里。",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "event_types_extra",
                                            "label": "自定义删除事件名（可选）",
                                            "placeholder": "留空即可；多个用英文逗号分隔",
                                            "hint": "上拉列表里没有的事件名写这里，会和勾选项一起生效。",
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
                                            "placeholder": "一行一个；媒体服务器路径包含该字符串即跳过（子串匹配，不是前缀）",
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
                                            "hint": "递归清理由转种/辅种插件记录的关联任务，并按下面的标签清理"
                                                    "CrossSeed（青蛙辅种助手）等「标签式」辅种插件建的种子。",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "seed_tags",
                                            "label": "辅种标签",
                                            "hint": "标签式辅种插件（CrossSeed / 青蛙辅种助手）给辅种种子打的标签，"
                                                    "默认「辅种」；多个用英文逗号分隔。插件会按"
                                                    "「标签命中 + 保存目录与该片一致」清理它们。",
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
                        "component": "VAlert",
                        "props": {
                            "type": "success",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": "配置 Emby：Emby 控制台 → 通知/Webhooks 新建一个 Webhook，地址填 "
                                    "http://你的MP地址/api/v1/webhook/?token=<MoviePilot 的 API 密钥>&source=Emby ，"
                                    "提交方式必须选 form-data（表单字段名 data）。MoviePilot 的 Emby 解析器只读表单字段 "
                                    "data、不读 JSON 请求体 —— 选 JSON 的话事件会被静默丢弃，插件收不到删除通知。",
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
            "event_types_extra": DEFAULT_EVENT_TYPES_EXTRA,
            "library_path": "",
            "exclude_path": "",
            "del_source": True,
            "del_seed": True,
            "seed_tags": DEFAULT_SEED_TAGS,
            "title_guard": True,
            "torrent_action": ACTION_DELETE,
        }

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件自身的只读接口。

        直投入口（``/emby_webhook``，匿名）已于 1.5.0 **移除**，事件只从宿主事件总线进入；
        这里只保留插件详情页要用的历史查询/清空接口，且一律要求宿主鉴权（``apikey``）。

        注：``_PluginBase`` 把 ``get_api`` 声明为 abstractmethod，插件必须实现它才能被实例化。
        """
        return [
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
        if not event:      # 启用开关、事件名与幂等判定统一在 _handle_event_data 内做，避免两处口径不一致
            return
        self._handle_event_data(event.event_data)

    def _handle_event_data(self, event_data: Any) -> None:
        """事件体处理链：宿主 webhook 事件与「媒体服务器直投 JSON」共用同一条逻辑。"""
        if not self._enabled or not event_data:
            return
        # 幂等：Emby 重投 / 代理重放 / 双入口同时到达时，同一事件只处理一次
        if self._event_seen_recently(event_data):
            self._record_event(
                "duplicate_ignored",
                f"重复事件已在 {EVENT_DEDUP_WINDOW} 秒内处理过，跳过：{getattr(event_data, 'event', '')}",
                str(getattr(event_data, "item_name", "") or ""),
            )
            return

        event_type = str(event_data.event or "")
        allowed = (parse_event_types(self._event_types)
                   + parse_event_types(getattr(self, "_event_types_extra", "")))
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
        # 未知/空类型一律按"非电影"的严格口径匹配：类型缺失时若按电影走"父目录即命中"，
        # 剧集单集事件会命中整季记录（实测 S01E01 的事件能匹配到整季 3 条记录）。
        is_tv = media_type.strip() not in MOVIE_ITEM_TYPES
        target_path = map_path(media_path, cached_mappings(self._library_path))
        if not self._library_path:
            logger.warning("联动删除：未配置媒体库路径映射，将直接使用媒体服务器路径匹配")

        records = self._find_records(
            media_name=media_name,
            target_path=target_path,
            media_source=media_source,
            media_id=media_id,
            is_tv=is_tv,
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
            if self._in_library_scope(target_path):
                self._notify_result(title="⚠️ 联动删除未找到整理记录", text=message,
                                    image=self._fallback_image())
            else:
                self._record_event(
                    "out_of_scope",
                    f"删除事件不在媒体库映射范围内（不告警，仅记录）：{target_path}",
                    media_name, media_id)
            return

        # 演练与实跑必须用**同一份**待处理清单：护栏（防误删校验）在分支之前统一应用，
        # 否则演练列出的内容与关掉演练后实际会删的内容不一致，"先出清单再确认"的流程就失效了。
        kept, guarded = [], []
        for record in records:
            if self._title_guard and not title_matches(getattr(record, "title", None), media_name):
                guarded.append(record)
                logger.warning(
                    f"联动删除：整理记录 {getattr(record, 'id', '?')}（{getattr(record, 'title', '')}）"
                    f"与删除媒体 {media_name} 不符，防误删已跳过"
                )
                continue
            kept.append(record)
        records = kept

        plan: List[Dict[str, Any]] = []
        results: List[str] = []
        error_cnt = 0

        guard_note = ""
        if guarded:
            guard_note = "\n".join(
                f"• 防误删跳过（标题与媒体名不一致）：记录 {getattr(r, 'id', '?')}（{getattr(r, 'title', '')}）"
                for r in guarded
            )
            results.append(guard_note)

        if not records:
            message = (
                f"联动删除：命中 {len(records) + len(guarded)} 条整理记录，但全部被「标题防误删校验」拦下"
                f"（媒体名：{media_name}），未执行任何删除。若确认无误请检查整理记录标题。"
            )
            logger.warning(message)
            self._record_event("guarded", message, media_name, media_id)
            self._notify_result(title="⚠️ 联动删除被防误删校验拦下", text=message,
                                image=self._fallback_image())
            return

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
            try:
                lines, ok = self._delete_record(record)
                results.extend(lines)
                if not ok:
                    error_cnt += 1
            except Exception as err:  # noqa: BLE001 - 单条失败不影响其余记录
                error_cnt += 1
                results.append(f"• 记录 {record.id} 处理失败：{err}")
                logger.error(f"联动删除：整理记录 {record.id} 处理失败：{err}")

        # 占位文件（strm）一并清理：只删占位、不碰真实媒体文件，避免媒体服务器下次扫描又把条目加回来
        if self._del_source:
            msg, ok = self._remove_placeholder(media_path)
            results.extend(msg)
            if not ok:
                error_cnt += 1

        self._append_history(media_name, media_id, results, error_cnt)
        if self._notify:
            head = "✅" if not error_cnt else "⚠️"
            body = "\n".join(results) if results else "无可删除内容"
            summary = f"成功 {len(records) - error_cnt} 条，失败 {error_cnt} 条"
            if guarded:
                summary += f"，防误删跳过 {len(guarded)} 条"
            self._notify_result(
                title=f"{head} 联动删除完成：{media_name}",
                text=f"{body}\n\n{summary}。",
                image=self._poster_image(records),
            )

    def _delete_record(self, record: Any) -> Tuple[List[str], bool]:
        """
        删除单条整理记录对应的文件与下载任务，最后删除记录本身。

        :return: (结果描述行, 整理记录是否已删除)。**任何一步没做干净都返回 False 并保留整理记录** ——
                 整理记录是清理失败后唯一的重试依据，删掉它就永久失去补删能力（用户还以为已经删完了）。
        """
        lines: List[str] = []
        allowed_roots = self._allowed_roots()
        cleanup_ok = True

        # 1、媒体库文件与源文件
        if self._del_source:
            if record.dest:
                if is_within(record.dest, allowed_roots):
                    msg, ok = self._remove_file(record.dest, "媒体库文件")
                    lines.extend(msg)
                    cleanup_ok = cleanup_ok and ok
                else:
                    cleanup_ok = False
                    lines.append(f"• 跳过媒体库文件（不在映射范围内，请检查路径映射）：{record.dest}")
                    logger.warning(
                        f"联动删除：媒体库文件 {record.dest} 不在映射范围内，已跳过；"
                        f"请检查「媒体库路径映射」配置"
                    )
            if record.src:
                _src_is_dir = os.path.isdir(str(record.src))
                _src_is_media = is_media_file(record.src)
                _src_is_aux = (not _src_is_media and not _src_is_dir
                               and self._is_aux_file(record.src))
                if not (_src_is_media or _src_is_dir or _src_is_aux):
                    cleanup_ok = False
                    lines.append(
                        f"• 跳过源文件（既不是媒体文件、目录，也不是与媒体同目录的附属文件）：{record.src}"
                    )
                elif not self._source_allowed(record.src):
                    cleanup_ok = False
                    lines.append(f"• 跳过源文件（不在允许的源文件根目录内）：{record.src}")
                    logger.warning(
                        f"联动删除：源文件 {record.src} 不在「源文件根目录」配置内，已跳过"
                    )
                else:
                    # 剪枝根 = 源文件所在目录的**上一级**：片子自己的源目录可以被清掉，
                    # 但绝不越过它继续往上删（保种根下还有别的片子）。
                    _src_dir = os.path.dirname(str(record.src))
                    msg, ok = self._remove_file(
                        record.src, "附属文件" if _src_is_aux else "源文件",
                        [os.path.dirname(_src_dir) or _src_dir])
                    lines.extend(msg)
                    cleanup_ok = cleanup_ok and ok
        # 2、下载任务（含转种与辅种）
        try:
            msg, ok = self._handle_torrents(record)
            lines.extend(msg)
            cleanup_ok = cleanup_ok and ok
        except Exception as err:  # noqa: BLE001
            cleanup_ok = False
            lines.append(f"• 处理下载任务异常：{err}")
            logger.error(f"联动删除：处理下载任务异常：{err}")

        # 3、整理记录最后删除：它是跨文件系统 / 下载器清理失败后的重试依据
        if cleanup_ok and self._transferhis:
            try:
                self._transferhis.delete(record.id)
            except Exception as err:  # noqa: BLE001
                lines.append(f"• 删除整理记录失败：{record.id}（{err}）")
                logger.error(f"联动删除：删除整理记录 {record.id} 失败：{err}")
                return lines, False
            lines.append(f"• 已删除整理记录：{record.id}（{record.title}）")
        else:
            lines.append(f"• 外部清理未完成（失败或跳过），保留整理记录以便重试：{record.id}")
        return lines, cleanup_ok

    @staticmethod
    def _root_reachable(target: Path, depth: int = 5) -> bool:
        """
        目标文件不存在时，判断它"本该在的目录"是否可达。

        用于区分两种情况（不区分会把"挂载缺失"当成"已经删过了"）：
        - 目录可达 → 幂等重放（之前删过了），可以判定清理已完成；
        - 目录不可达 → 挂载缺失/路径映射配错，文件根本没被删，必须判失败并保留整理记录。
        """
        current = target.parent
        for _ in range(depth):
            try:
                if current.is_dir():
                    return True
            except Exception:  # noqa: BLE001
                return False
            if current.parent == current:
                return False
            current = current.parent
        return False

    def _is_aux_file(self, path: str) -> bool:
        """是否为"可以一并删掉的附属文件"（字幕/外挂音轨/元数据/图片）。

        **双条件**，缺一不可：
          ① 扩展名在 ``AUX_EXTS`` 白名单内；
          ② 该文件所在目录里**确实存在媒体文件**（说明它是某个片源的附属文件，
             而不是通用目录里的同名杂物）。
        任一条件不满足即返回 False（fail-closed），宁可不删也不误删。
        """
        p = Path(str(path).replace("\\", "/"))
        if p.suffix.lower().lstrip(".") not in AUX_EXTS:
            return False
        try:
            for child in p.parent.iterdir():
                if child.is_file() and is_media_file(str(child)):
                    return True
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：判定附属文件失败（按不删处理）：{err}")
            return False
        return False

    def _remove_file(
            self, path: str, label: str, prune_roots: Optional[List[str]] = None
    ) -> Tuple[List[str], bool]:
        """
        删除文件（或目录）并清理空的父目录。

        :return: (结果描述行, 是否确认清理完成)
        """
        lines: List[str] = []
        target = Path(str(path).replace("\\", "/"))
        try:
            if target.is_dir():
                shutil.rmtree(target)
                lines.append(f"• 已删除{label}（目录）：{path}")
                self._prune_empty_dirs(target.parent, prune_roots)
                return lines, True
            if target.exists():
                target.unlink()
                lines.append(f"• 已删除{label}：{path}")
                self._prune_empty_dirs(target.parent, prune_roots)
                return lines, True
            # 不存在：区分"已经被删过（幂等）"与"根本看不见（挂载缺失/映射配错）"
            if self._root_reachable(target):
                lines.append(f"• {label}不存在（可能已删除过），已跳过：{path}")
                return lines, True
            lines.append(
                f"• 删除{label}失败：{path}（所在目录在 MoviePilot 内不可见，请检查挂载与路径映射）"
            )
            logger.error(f"联动删除：{label} {path} 所在目录不可见，判定为未清理")
            return lines, False
        except Exception as err:  # noqa: BLE001
            lines.append(f"• 删除{label}失败：{path}（{err}）")
            logger.error(f"联动删除：删除{label} {path} 失败：{err}")
            return lines, False

    def _remove_placeholder(self, media_path: str) -> Tuple[List[str], bool]:
        """
        清理媒体服务器侧的占位文件（strm）。

        只处理 .strm 占位文件：真实媒体文件由整理记录里的路径负责删除，两者互不重复。
        占位文件不清理时，媒体服务器下次扫描会把条目重新加回媒体库。
        """
        if not media_path or Path(str(media_path)).suffix.lower() != ".strm":
            return [], True
        server_roots = [server for server, _ in cached_mappings(self._library_path)]
        # fail-closed：没配置媒体库路径映射时**不能**跳过这道护栏 ——
        # 否则事件里的任意 .strm 路径都会被无条件删除（实测可删到映射范围外）。
        if not server_roots:
            return [f"• 跳过占位文件（未配置媒体库路径映射，无法校验路径范围）：{media_path}"], True
        if not is_within(media_path, server_roots):
            return [f"• 跳过占位文件（不在媒体服务器映射范围内）：{media_path}"], True
        target = Path(str(media_path).replace("\\", "/"))
        if not target.exists():
            # MP 容器内看不到媒体服务器挂载是环境事实（不是失败）→ 只提示、不阻塞
            return [f"• 占位文件不存在或未挂载到 MoviePilot，已跳过：{media_path}"], True
        return self._remove_file(media_path, "占位文件(strm)")

    def _prune_empty_dirs(self, directory: Path, roots: Optional[List[str]] = None) -> None:
        """自底向上删除空目录，遇到非空目录、受保护目录或超出给定根即停止。

        :param roots: 允许剪枝的根目录；默认用媒体库映射目标。**根为空时一律不剪枝**（fail-closed）。
        """
        protected = {Path(os.path.abspath(str(root).replace("\\", "/")))
                     for root in (self._allowed_roots() if roots is None else roots)}
        # 没有受保护根（未配置映射）时**一律不剪枝**：否则 protected 为空集，
        # 会沿父目录一路 rmdir 到媒体库之外（最多 MAX_PRUNE_DEPTH 层）。
        if not protected:
            return
        current = Path(os.path.abspath(str(directory).replace("\\", "/")))
        if not any(current == root or str(current).startswith(str(root).rstrip("/") + "/")
                   or str(root).startswith(str(current).rstrip("/") + "/")
                   for root in protected):
            return
        for _ in range(MAX_PRUNE_DEPTH):
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
            is_tv: bool,
    ) -> List[Any]:
        """
        查找与删除事件匹配的整理记录。

        顺序：身份精确查询 → 目录 / 文件名主干兜底；无论走哪条路径，最终都要求
        记录与事件指向同一份媒体（见 :func:`same_media`），因此后缀差异不影响匹配。
        """
        candidates: List[Any] = []

        # ① 按媒体身份查询（身份缺失时不要拿字符串 "None" 去查，那只会污染候选集）
        if media_source and media_id and str(media_id) not in ("None", ""):
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
        matched = [r for r in candidates
                   if same_media(r.dest, target_path, is_tv,
                                 getattr(r, "title", None), media_name)]

        # ③ 仍未命中时，用映射后的目录名做文本包含查询兜底
        if not matched:
            # 顺序固定（原先用 set，迭代顺序依赖哈希，匹配结果不稳定）：先精确后宽松
            keywords = []
            for _kw in (media_name,
                        Path(target_path).stem if target_path else "",
                        Path(target_path).parent.name if target_path else ""):
                if _kw and _kw not in keywords:
                    keywords.append(_kw)
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
                    if same_media(record.dest, target_path, is_tv,
                                  getattr(record, "title", None), media_name):
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

    def _handle_torrents(self, record: Any) -> Tuple[List[str], bool]:
        """处理整理记录关联的下载任务：主任务 + 转种 + 辅种。返回 (结果行, 是否全部处理成功)。"""
        lines: List[str] = []
        torrent_hash = getattr(record, "download_hash", None)
        if not torrent_hash:
            return lines, True
        if not self.chain:
            # 清理链不可用时不能装作"没有下载任务"：否则整理记录被删、任务却留在下载器里，
            # 既没清掉也失去重试依据。
            lines.append(f"• 下载任务未处理（清理链不可用）：{torrent_hash}")
            logger.error(f"联动删除：清理链不可用，下载任务 {torrent_hash} 未处理")
            return lines, False
        downloader = self._downloader_of(torrent_hash) or self._default_downloader
        ok_all = True
        msg, ok = self._handle_one_torrent(str(torrent_hash), downloader, self._torrent_action)
        lines.extend(msg)
        ok_all = ok_all and ok
        if self._del_seed:
            msg, ok = self._handle_seeds(str(torrent_hash), set())
            lines.extend(msg)
            ok_all = ok_all and ok
            msg, ok = self._handle_tagged_seeds(record)
            lines.extend(msg)
            ok_all = ok_all and ok
        return lines, ok_all

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
    ) -> Tuple[List[str], bool]:
        """按配置删除或暂停一个下载任务，并同步处理转种记录。返回 (结果行, 是否成功)。"""
        lines: List[str] = []
        if not torrent_hash:
            return lines, True
        ok_all = True

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
                msg, ok = self._act_torrent(str(target_hash), target_downloader, action, "转种任务")
                lines.extend(msg)
                ok_all = ok_all and ok
            source_downloader = transfer.get("from_download")
            source_hash = transfer.get("from_download_id")
            if source_hash and source_hash != torrent_hash:
                msg, ok = self._act_torrent(str(source_hash), source_downloader, action, "源下载任务")
                lines.extend(msg)
                ok_all = ok_all and ok

        msg, ok = self._act_torrent(torrent_hash, downloader, action)
        lines.extend(msg)
        ok_all = ok_all and ok
        return lines, ok_all

    def _act_torrent(
            self,
            torrent_hash: str,
            downloader: Optional[str],
            action: str,
            label: str = "下载任务",
    ) -> Tuple[List[str], bool]:
        """对下载器执行删除 / 暂停。返回 (结果行, 是否成功)。"""
        if not torrent_hash:
            return [], True
        if not self.chain:
            return [f"• {label}未处理（清理链不可用）：{torrent_hash}"], False
        try:
            if action == ACTION_STOP:
                self.chain.stop_torrents(hashs=torrent_hash, downloader=downloader)
                return [f"• 已暂停{label}：{torrent_hash}"], True
            self.chain.remove_torrents(hashs=torrent_hash, delete_file=True, downloader=downloader)
            return [f"• 已删除{label}：{torrent_hash}"], True
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：处理{label} {torrent_hash} 失败：{err}")
            return [f"• 处理{label}失败：{torrent_hash}（{err}）"], False

    def _tagged_seed_hashes(self, record: Any) -> List[Tuple[str, str]]:
        """按标签找出"标签式辅种"（CrossSeed / 青蛙辅种助手 等）。

        这类插件的辅种关系**不落库**，只体现为下载器里种子的标签（默认「辅种」）。它们与主体是
        同一份文件，主体被删后必然变成死种，所以要一并清掉。定位判据用**两个条件同时满足**：
        ① 种子带有配置的辅种标签；② 保存目录与该影片的源文件目录（或库文件目录）一致 ——
        只靠标签会把别的影片的辅种一起删掉。

        :return: [(下载器名, 种子 hash), ...]
        """
        tags = [item.strip() for item in str(getattr(self, "_seed_tags", "") or "").split(",")
                if item.strip()]
        helper = getattr(self, "_downloader_helper", None)
        if not tags or not helper:
            return []
        target_dirs = set()
        for candidate in (getattr(record, "src", None), getattr(record, "dest", None)):
            if candidate:
                target_dirs.add(os.path.dirname(str(candidate).replace("\\", "/")))
        if not target_dirs:
            return []
        try:
            services = helper.get_services() or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"联动删除：读取下载器服务失败，跳过标签式辅种清理：{err}")
            return []
        found: List[Tuple[str, str]] = []
        for name, service in services.items():
            lister = getattr(service, "list_torrents", None)
            if not callable(lister):
                continue
            try:
                torrents = lister()
            except Exception as err:  # noqa: BLE001
                logger.warning(f"联动删除：列举 {name} 的种子失败：{err}")
                continue
            for item in torrents or []:
                if not isinstance(item, dict):
                    continue
                item_tags = item.get("tags") or []
                if isinstance(item_tags, str):
                    item_tags = [x.strip() for x in item_tags.split(",")]
                if not any(tag in item_tags for tag in tags):
                    continue
                save = str(item.get("save_path") or item.get("content_path") or "").replace("\\", "/")
                if not save:
                    continue
                if not any(save == d or save.startswith(d.rstrip("/") + "/") for d in target_dirs):
                    continue
                item_hash = str(item.get("hash") or "")
                if item_hash and (str(name), item_hash) not in found:
                    found.append((str(name), item_hash))
        return found

    def _handle_tagged_seeds(self, record: Any) -> Tuple[List[str], bool]:
        """清理标签式辅种（CrossSeed 等），返回 (结果行, 是否成功)。"""
        lines: List[str] = []
        ok_all = True
        for downloader, seed_hash in self._tagged_seed_hashes(record):
            msg, ok = self._act_torrent(seed_hash, downloader, self._torrent_action,
                                        f"标签式辅种[{downloader}]")
            lines.extend(msg)
            ok_all = ok_all and ok
        return lines, ok_all

    def _handle_seeds(self, torrent_hash: str, visited: set, depth: int = 0) -> Tuple[List[str], bool]:
        """递归处理辅种记录（读取辅种插件保存的关联任务）。返回 (结果行, 是否成功)。"""
        lines: List[str] = []
        if depth >= MAX_SEED_DEPTH or not torrent_hash or torrent_hash in visited:
            return lines, True
        visited.add(torrent_hash)
        try:
            seed_history = self.get_data(key=torrent_hash, plugin_id="IYUUAutoSeed") or []
        except Exception as err:  # noqa: BLE001
            # 区分"未安装辅种插件"（正常）与"读取真实失败"（要留痕，否则用户以为辅种已清）
            logger.warning(f"联动删除：读取辅种记录 {torrent_hash} 失败：{err}")
            return lines, True
        if not isinstance(seed_history, list):
            return lines, True
        ok_all = True
        for item in seed_history:
            if not isinstance(item, dict):
                continue
            downloader = item.get("downloader")
            torrents = item.get("torrents")
            if isinstance(torrents, str):
                torrents = [torrents]
            for seed_hash in torrents or []:
                msg, ok = self._act_torrent(str(seed_hash), downloader, self._torrent_action, "辅种任务")
                lines.extend(msg)
                ok_all = ok_all and ok
                msg, ok = self._handle_seeds(str(seed_hash), visited, depth + 1)
                lines.extend(msg)
                ok_all = ok_all and ok
        return lines, ok_all

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def _state_lock(self) -> threading.Lock:
        """实例级锁：审计历史与幂等缓存的读-改-写必须是原子的（并发事件会互相覆盖）。"""
        lock = getattr(self, "_state_lock_obj", None)
        if lock is None:
            lock = threading.Lock()
            self._state_lock_obj = lock
        return lock

    def _event_seen_recently(self, event_data: Any) -> bool:
        """同一事件在 EVENT_DEDUP_WINDOW 秒内重复到达时返回 True（幂等）。"""
        key = "|".join([
            str(getattr(event_data, "event", "") or ""),
            str(getattr(event_data, "item_name", "") or ""),
            str(getattr(event_data, "item_path", "") or ""),
        ])
        if not key.replace("|", "").strip():
            return False
        now = time.time()
        with self._state_lock():
            cache = getattr(self, "_recent_events", None)
            if not isinstance(cache, dict):
                cache = {}
                self._recent_events = cache
            for stale in [k for k, ts in cache.items() if now - ts > EVENT_DEDUP_WINDOW]:
                cache.pop(stale, None)
            if key in cache:
                return True
            cache[key] = now
        return False

    def _state_lock(self) -> threading.Lock:
        """实例级锁：审计历史与幂等缓存的读-改-写必须是原子的（并发事件会互相覆盖）。"""
        lock = getattr(self, "_state_lock_obj", None)
        if lock is None:
            lock = threading.Lock()
            self._state_lock_obj = lock
        return lock

    def _event_seen_recently(self, event_data: Any) -> bool:
        """同一事件在 EVENT_DEDUP_WINDOW 秒内重复到达时返回 True（幂等）。"""
        key = "|".join([
            str(getattr(event_data, "event", "") or ""),
            str(getattr(event_data, "item_name", "") or ""),
            str(getattr(event_data, "item_path", "") or ""),
        ])
        if not key.replace("|", "").strip():
            return False
        now = time.time()
        with self._state_lock():
            cache = getattr(self, "_recent_events", None)
            if not isinstance(cache, dict):
                cache = {}
                self._recent_events = cache
            for stale in [k for k, ts in cache.items() if now - ts > EVENT_DEDUP_WINDOW]:
                cache.pop(stale, None)
            if key in cache:
                return True
            cache[key] = now
        return False

    def _source_allowed(self, path: str) -> bool:
        """源文件是否允许删除。

        配置了「源文件根目录」(source_roots) 时**必须**落在其中一个根目录内；
        未配置时保持兼容（放行）—— 源文件路径来自 MoviePilot 自己的整理记录，属可信来源，
        需要更严格约束时再填这个配置。
        """
        roots = [line.strip() for line in str(getattr(self, "_source_roots", "") or "").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if not roots:
            return True
        return is_within(path, roots)

    def _allowed_roots(self) -> List[str]:
        """允许删除的根目录：**只包含媒体库映射**的 MoviePilot 侧目标。

        源文件（整理记录的 src）走单独的可选配置 ``source_roots``，见 :meth:`_source_allowed`。
        """
        roots = [local for _, local in cached_mappings(self._library_path)]
        return roots

    def _source_allowed(self, path: str) -> bool:
        """源文件是否允许删除。

        配置了「源文件根目录」(source_roots) 时**必须**落在其中一个根目录内；
        未配置时保持兼容（放行），但会在结果里提示 —— 源文件路径来自 MoviePilot 自己的整理记录，
        属可信来源，需要更严格约束时再填这个配置。
        """
        roots = [line.strip() for line in str(getattr(self, "_source_roots", "") or "").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if not roots:
            return True
        return is_within(path, roots)

    def _excluded(self, media_path: str) -> bool:
        """判断媒体路径是否命中排除规则（**子串包含**，不是前缀）。

        注意作用对象是**媒体服务器上报的原始路径**（未做映射），配置时按媒体服务器侧路径填写。
        """
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

    def _in_library_scope(self, target_path: str) -> bool:
        """事件映射后的路径是否落在媒体库映射范围内。

        不在范围内（例如 JAV 库这类不归 MoviePilot 管的目录）时，本插件本就不会有任何
        整理记录可清理，没必要每次都发"未找到整理记录"告警 —— 只记日志，保持通知干净。
        """
        try:
            roots = [local for _, local in cached_mappings(self._library_path)]
        except Exception as err:  # noqa: BLE001
            logger.error(f"联动删除：读取路径映射失败：{err}")
            return True  # 读不到映射时保持原有告警行为（宁可多说一句，不可静默漏报）
        if not roots:
            return True
        norm = str(target_path or "")
        return any(norm.startswith(str(r).rstrip("/")) for r in roots if r)

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
            self.post_message(mtype=mtype, title=f"【Emby联动删除】{title}", text=text, image=image)
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
