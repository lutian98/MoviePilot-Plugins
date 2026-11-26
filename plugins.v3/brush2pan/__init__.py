# -*- coding: utf-8 -*-
"""
刷流直传 115（Brush2Pan）

刷流（BrushFlow）下载完成的影片，按口径识别/筛选后，通过 CD2 gRPC 的 CopyFile
把本地文件直接上传到 115 固定目录，并按 TMDB 名称重命名（MP 风格）。

- 不上传剧集、不收蓝光原盘（BDMV/m2ts/ISO）
- 只收 1080p / 2160p
- 排除 5.1 / 7.1 / Atmos / TrueHD / DTS-HD 多声道
- 上传由 CD2 服务端完成（读取 /Unraid 本地文件 → /115），不经过本插件搬运
- dry_run 默认开启，只出清单不复制

依赖：MP 自带 requests / qbittorrent-api；grpcurl 二进制首次运行时下载到插件数据目录。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".wmv", ".flv", ".webm"}
AUDIO_BAD = re.compile(r"(?i)(5\.1|7\.1|atmos|truehd|dts[-\s]?hd|dts[-\s]?x)")
MARKER_RE = re.compile(
    r"(?i)(?:S\d{2,3}(?:E\d{1,3})?|\b(?:19|20)\d{2}\b|2160p|1080p|720p|480p|"
    r"WEB[ -]?DL|WEBRip|BluRay|Blu-ray|HDTV|REMUX|UHD|HDR|DoVi|DV|"
    r"HEVC|AVC|x264|x265|H\.?264|H\.?265|AAC|DDP|DD\+|DD|Atmos|TrueHD|DTS|FLAC|"
    r"10bit|8bit|REPACK|PROPER|Extended|Unrated)"
)

GRPCURL_VERSION = "1.9.1"
GRPCURL_URL = (
    f"https://github.com/fullstorydev/grpcurl/releases/download/v{GRPCURL_VERSION}/"
    f"grpcurl_{GRPCURL_VERSION}_linux_x86_64.tar.gz"
)


class Brush2Pan(_PluginBase):
    # 插件元数据
    plugin_name = "刷流直传 115"
    plugin_desc = "刷流完成的影片按口径筛选后，经 CD2 直传 115 固定目录并按 TMDB 重命名"
    plugin_icon = "brush2pan.png"
    plugin_version = "0.1.7"
    plugin_author = "lutian98"
    author_url = ""
    plugin_config_prefix = "brush2pan_"
    plugin_order = 90
    auth_level = 1

    _enabled: bool = False
    _dry_run: bool = True
    _cd2_grpc: str = ""
    _cd2_token: str = ""
    _dest_root: str = ""
    _src_prefix: str = ""
    _save_prefix: str = "/downloads"
    _min_size_mb: int = 300
    _tmdb_key: str = ""
    _require_chs: bool = True

    def __init__(self) -> None:
        super().__init__()
        self._downloader_helper = DownloaderHelper()

    # ---------------- 生命周期 ----------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or self.get_config() or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._dry_run = bool(cfg.get("dry_run", True))
        self._cd2_grpc = str(cfg.get("cd2_grpc") or "")
        self._cd2_token = str(cfg.get("cd2_token") or "")
        self._dest_root = str(cfg.get("dest_root") or "/115/媒体/影片").rstrip("/")
        self._src_prefix = str(cfg.get("src_prefix") or "/Unraid/Media/刷流").rstrip("/")
        self._save_prefix = str(cfg.get("save_path") or "/downloads/刷流").rstrip("/")
        try:
            self._min_size_mb = int(cfg.get("min_size_mb") or 300)
        except Exception:
            self._min_size_mb = 300
        self._tmdb_key = str(cfg.get("tmdb_key") or getattr(settings, "TMDB_API_KEY", "") or "")
        self._require_chs = bool(cfg.get("require_chs", True))
        logger.info(f"刷流直传：init enabled={self._enabled} dry_run={self._dry_run} dest={self._dest_root}")

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/probe",
            "endpoint": self.api_probe,
            "methods": ["GET"],
            "summary": "识别测试",
            "description": "传入 release 文件名，返回解析/TMDB/命名结果，不复制",
        }, {
            "path": "/subcheck",
            "endpoint": self.api_subcheck,
            "methods": ["GET"],
            "summary": "中字检测",
            "description": "传入种子内视频相对路径，检测外挂/内嵌中字",
        }, {
            "path": "/selftest",
            "endpoint": self.api_selftest,
            "methods": ["GET"],
            "summary": "CD2 连通性自检",
            "description": "检查 grpcurl/协议/Token 是否可用，返回 CD2 挂载点，不复制任何文件",
        }]

    def api_subcheck(self, name: str = ""):
        if not name or ".." in name:
            return {"ok": False, "error": "name 必须是种子内相对路径"}
        video = {"name": name, "size": 0}
        ok, reason = self._has_chs(video, [], None)
        return {"ok": ok, "reason": reason, "path": os.path.join(self._save_prefix, name.lstrip("/"))}

    def api_selftest(self):
        if not self._cd2_token:
            return {"ok": False, "error": "未配置 CD2 Token"}
        try:
            data = self._cd2("GetMountPoints")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}
        mounts = data.get("mountPoints") or []
        return {"ok": True, "mounts": [{"name": m.get("name"), "mountPoint": m.get("mountPoint"),
                                        "sourceDir": m.get("sourceDir"), "isMounted": m.get("isMounted")}
                                       for m in mounts]}

    def api_probe(self, name: str = ""):
        meta = self._parse_release(name)
        tmdb = self._tmdb_search(meta["cn"] or meta["title"], meta["year"], meta["is_tv"])
        out: Dict[str, Any] = {"input": name, "meta": meta,
                               "tmdb": None if not tmdb else {
                                   "id": tmdb.get("id"),
                                   "title": tmdb.get("title") or tmdb.get("name"),
                                   "date": tmdb.get("release_date") or tmdb.get("first_air_date")}}
        if tmdb:
            year, folder, fname = self._build_names(meta, tmdb, {"name": name})
            out["year"], out["folder"], out["target_name"] = year, folder, fname
        return out

    def get_page(self) -> Optional[List[dict]]:
        return []

    def stop_service(self) -> None:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        return [{
            "id": "Brush2Pan",
            "name": "刷流直传 115",
            "trigger": "interval",
            "func": self.scan,
            "kwargs": {"seconds": 60},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {
                                "model": "dry_run", "label": "演练模式（只判定不复制）",
                                "hint": "首次启用建议开启，只写日志/台账，不调用 CD2 CopyFile。",
                                "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {
                                "model": "require_chs", "label": "必须含中字",
                                "hint": "检查外挂字幕文件 / 内嵌字幕流 / 文件名标注；未发现中字则拒绝。",
                                "persistent-hint": True}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "cd2_grpc", "label": "CD2 gRPC 地址",
                                "placeholder": "cd2:19798"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "cd2_token", "label": "CD2 API Token",
                                "type": "password", "hint": "CD2 设置 → API 令牌，需读写/本地列目录权限",
                                "persistent-hint": True}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "src_prefix", "label": "本地源路径（CD2 视角）",
                                "placeholder": "/Unraid/Media/刷流"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "dest_root", "label": "115 目标目录",
                                "placeholder": "/115/媒体/影片"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "save_path", "label": "QB 保存路径过滤",
                                "placeholder": "/downloads/刷流"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "min_size_mb", "label": "最小体积（MB）",
                                "type": "number", "placeholder": "300"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "tmdb_key", "label": "TMDB API Key（留空用 MP 内置）",
                                "type": "password"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False,
            "dry_run": True,
            "require_chs": True,
            "cd2_grpc": "",
            "cd2_token": "",
            "src_prefix": "/Unraid/Media/刷流",
            "dest_root": "/115/媒体/影片",
            "save_path": "/downloads/刷流",
            "min_size_mb": 300,
            "tmdb_key": "",
        }

    # ---------------- 工具 ----------------
    def _data_dir(self) -> Path:
        p = self.get_data_path()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _proxies(self) -> Optional[dict]:
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            return None
        if isinstance(proxy, dict):
            return proxy
        return {"http": str(proxy), "https": str(proxy)}

    def _ensure_grpcurl(self) -> Optional[str]:
        data = self._data_dir()
        bin_path = data / "grpcurl"
        if bin_path.exists() and os.access(bin_path, os.X_OK):
            return str(bin_path)
        proxy = getattr(settings, "GITHUB_PROXY", "") or ""
        urls = [url for url in (GRPCURL_URL, proxy.rstrip("/") + "/" + GRPCURL_URL) if url]
        for url in urls:
            try:
                logger.info(f"刷流直传：下载 grpcurl ← {url}")
                resp = requests.get(url, timeout=120, proxies=self._proxies())
                resp.raise_for_status()
                tar_path = data / "grpcurl.tar.gz"
                tar_path.write_bytes(resp.content)
                with tarfile.open(tar_path) as tf:
                    member = next(m for m in tf.getmembers() if m.name.endswith("/grpcurl") or m.name == "grpcurl")
                    member.name = "grpcurl"
                    tf.extract(member, path=data)
                tar_path.unlink(missing_ok=True)
                os.chmod(bin_path, 0o755)
                return str(bin_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"刷流直传：grpcurl 下载失败 {url}: {e}")
        return None

    def _cd2(self, method: str, payload: Optional[dict] = None, timeout: int = 60) -> dict:
        exe = self._ensure_grpcurl()
        if not exe:
            raise RuntimeError("grpcurl 不可用")
        proto = Path(__file__).parent / "clouddrive.proto"
        cmd = [exe, "-plaintext", "-import-path", str(proto.parent), "-proto", str(proto),
               "-H", f"authorization: Bearer {self._cd2_token}",
               "-max-time", str(int(timeout))]
        if payload is not None:
            cmd += ["-d", json.dumps(payload, ensure_ascii=False)]
        cmd += [self._cd2_grpc, f"clouddrive.CloudDriveFileSrv/{method}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            raise RuntimeError((r.stderr or out)[:300])
        if out.startswith("ERROR"):
            raise RuntimeError(out[:300])
        return json.loads(out) if out else {}

    def _cd2_find(self, parent: str, path: str) -> Optional[dict]:
        try:
            return self._cd2("FindFileByPath", {"parentPath": parent, "path": path})
        except Exception:
            return None

    # ---------------- 识别 / 判定 ----------------
    @staticmethod
    def _parse_release(filename: str) -> dict:
        stem = os.path.splitext(os.path.basename(filename))[0]
        cn = None
        m = re.match(r"^\[([^\]]+)\]", stem)
        stem2 = stem
        if m:
            cn = m.group(1).strip()
            stem2 = stem[m.end():]
        ym = re.search(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])", stem2)
        year = int(ym.group(1)) if ym else None
        rm = re.search(r"(?i)(2160p|1080p|720p|480p)", stem2)
        res = rm.group(1).lower() if rm else None
        tv = bool(re.search(r"(?i)S\d{2,3}(E\d{1,3})?", stem2))
        cut = MARKER_RE.search(stem2)
        raw = stem2[:cut.start()] if cut else stem2
        raw = re.sub(r"[._]+", " ", raw).strip(" -[]()")
        raw = re.sub(r"\s+", " ", raw)
        return {"title": cn or raw, "year": year, "resolution": res, "is_tv": tv, "cn": cn}

    @staticmethod
    def _title_variants(title: str) -> List[str]:
        """中英混排标题拆出中文/英文候选，提升 TMDB 命中率。"""
        out = [title]
        cjk = "".join(ch for ch in title if "\u4e00" <= ch <= "\u9fff")
        if cjk and cjk != title:
            out.append(cjk)
        non_cjk = re.sub(r"[\u4e00-\u9fff]+", " ", title)
        non_cjk = re.sub(r"\s+", " ", non_cjk).strip(" -_")
        if non_cjk and non_cjk != title:
            out.append(non_cjk)
        return [x for x in out if x]

    def _tmdb_search(self, title: str, year: Optional[int], is_tv: bool) -> Optional[dict]:
        if not self._tmdb_key:
            logger.warning("刷流直传：没有 TMDB key")
            return None
        for variant in self._title_variants(title) or [title]:
            result = self._tmdb_search_one(variant, year, is_tv)
            if result:
                return result
        return None

    def _tmdb_search_one(self, title: str, year: Optional[int], is_tv: bool) -> Optional[dict]:
        path = "search/tv" if is_tv else "search/movie"
        params = {"api_key": self._tmdb_key, "query": title, "language": "zh-CN"}
        if year:
            params["first_air_date_year" if is_tv else "year"] = year
        try:
            r = requests.get("https://api.themoviedb.org/3/" + path, params=params,
                             timeout=20, proxies=self._proxies())
            data = r.json()
            results = data.get("results") or []
            if not results:
                params.pop("year", None)
                params.pop("first_air_date_year", None)
                r = requests.get("https://api.themoviedb.org/3/" + path, params=params,
                                 timeout=20, proxies=self._proxies())
                results = r.json().get("results") or []
            best = None
            for item in results:
                name = item.get("title") or item.get("name") or ""
                date = item.get("release_date") or item.get("first_air_date") or ""
                ry = int(date[:4]) if date[:4].isdigit() else None
                score = 5 if name.strip().lower() == title.strip().lower() else 0
                if year and ry == year:
                    score += 5
                elif year and ry and abs(ry - year) <= 1:
                    score += 2
                if best is None or score > best[0]:
                    best = (score, item)
            return best[1] if best else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷流直传：TMDB 查询失败 {title}: {e}")
            return None

    SUB_EXT = {".srt", ".ass", ".ssa", ".sup", ".sub", ".idx"}

    @staticmethod
    def _cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    def _has_chs(self, video: dict, files: List[Any], meta: Optional[dict] = None) -> Tuple[bool, str]:
        """判断是否含中字：外挂字幕（读文本）→ 文件名标注 → 内嵌流（标签/抽文本/中文片兜底）。"""
        name = video.get("name", "")
        path = os.path.join(self._save_prefix, name.lstrip("/"))
        for f in files:
            fname = getattr(f, "name", "")
            if os.path.splitext(fname)[1].lower() in self.SUB_EXT:
                try:
                    blob = open(os.path.join(self._save_prefix, fname.lstrip("/")), "rb").read(65536)
                    if self._cjk(blob.decode("utf-8", "ignore")):
                        return True, f"外挂中文字幕 {os.path.basename(fname)}"
                except Exception:
                    pass
        if re.search(r"(中字|中文字幕|简繁|简中|繁中|简体|繁体|CHS|CHT|Chinese)", name, re.I):
            return True, "文件名标注中字"
        ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
                 "-select_streams", "s", path],
                capture_output=True, text=True, timeout=60)
            streams = json.loads(r.stdout or "{}").get("streams", [])
        except Exception as e:  # noqa: BLE001
            return False, f"ffprobe 执行失败: {e}"
        if not streams:
            return False, "未发现中文字幕（无外挂、无内嵌字幕流）"
        text_codecs = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt", "text"}
        image_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
        for st in streams:
            tags = st.get("tags") or {}
            lang = str(tags.get("language") or "").lower()
            title = str(tags.get("title") or "")
            if lang in ("chi", "zho", "zh", "cmn", "yue", "chs", "cht") or \
                    re.search(r"(中|简|繁|CHS|CHT|Chinese)", title, re.I):
                return True, f"内嵌字幕 {lang or title or st.get('codec_name','')}"
        # 文本字幕：抽第一条转 srt，看是否含 CJK
        ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
        if any((st.get("codec_name") in text_codecs) for st in streams):
            try:
                r = subprocess.run([ffmpeg, "-v", "quiet", "-i", path,
                                    "-map", "0:s:0", "-f", "srt", "-"],
                                   capture_output=True, text=True, timeout=120)
                if self._cjk(r.stdout[:200000]):
                    return True, "内嵌文本字幕含中文"
            except Exception:
                pass
        # 图形字幕（PGS 等）无法抽文本：中文片默认有中字
        if meta and (meta.get("tmdb_lang") in ("zh", "cn", "yue", "cmn")
                     or self._cjk(meta.get("tmdb_title") or "")):
            return True, "图形字幕未标注，中文片默认中字"
        if any(st.get("codec_name") in image_codecs for st in streams):
            return False, "图形字幕（PGS）无中文标记，非中文片"
        return False, f"内嵌字幕 {len(streams)} 条，但未检出中文"

    def _judge(self, meta: dict, video: dict, chs_ok: bool = True, chs_reason: str = "") -> Tuple[bool, str]:
        name = video.get("name", "")
        nl = name.lower()
        size_mb = (video.get("size") or 0) / 1024 / 1024
        if meta["is_tv"]:
            return False, "剧集（口径：不收剧集）"
        if "/bdmv/" in nl or nl.endswith(".m2ts") or nl.endswith(".iso") or "bdmv" in nl:
            return False, "蓝光原盘（口径：不收）"
        if not re.search(r"(?i)(1080p|2160p)", name):
            return False, "非 1080p/2160p"
        if AUDIO_BAD.search(name):
            return False, "多声道 5.1/7.1/Atmos/TrueHD/DTS-HD（口径：排除）"
        if size_mb < self._min_size_mb:
            return False, f"文件过小 {size_mb:.0f}MB < {self._min_size_mb}MB"
        if "sample" in nl:
            return False, "sample"
        if self._require_chs and not chs_ok:
            return False, f"无中字（{chs_reason}）"
        return True, "通过（中字✓；题材/分级口径待补）"

    @staticmethod
    def _build_names(meta: dict, tmdb: dict, video: dict) -> Tuple[int, str, str]:
        title = (tmdb.get("title") or tmdb.get("name") or meta["title"]).strip()
        date = tmdb.get("release_date") or tmdb.get("first_air_date") or ""
        year = int(date[:4]) if date[:4].isdigit() else meta["year"]
        fmt = meta["resolution"] or "1080p"
        ext = os.path.splitext(video.get("name", ""))[1].lower()
        if ext not in VIDEO_EXT:
            ext = ".mkv"
        folder = f"{title} ({year})"
        fname = f"{title} ({year}) - {fmt}{ext}"
        return year, folder, fname

    # ---------------- 主流程 ----------------
    def _qb_client(self):
        try:
            services = self._downloader_helper.get_services(type_filter="qbittorrent") or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷流直传：读取下载器失败: {e}")
            return None
        if not services:
            return None
        chosen = None
        for svc in services.values():
            if getattr(getattr(svc, "config", None), "default", False):
                chosen = svc
                break
        chosen = chosen or next(iter(services.values()))
        conf = getattr(getattr(chosen, "config", None), "config", None) or {}
        host = conf.get("host") or ""
        if not host:
            return None
        try:
            import qbittorrentapi
            return qbittorrentapi.Client(host=host, username=conf.get("username"),
                                         password=conf.get("password"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷流直传：连接 QB 失败: {e}")
            return None

    def scan(self) -> None:
        if not self._enabled:
            return
        client = self._qb_client()
        if not client:
            logger.warning("刷流直传：没有可用的 qbittorrent 下载器")
            return
        state: dict = self.get_data("state_v2") or {}
        try:
            torrents = client.torrents_info()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷流直传：列举种子失败: {e}")
            return
        for t in torrents:
            save_path = getattr(t, "save_path", "") or ""
            if not save_path.startswith(self._save_prefix):
                continue
            h = getattr(t, "hash", "")
            rec = state.get(h)
            if rec and rec.get("status") in ("done", "review", "error"):
                continue
            if rec and rec.get("status") == "copying":
                self._check_copying(rec)
                self.save_data("state_v2", state)
                continue
            if (getattr(t, "progress", 0) or 0) < 0.999:
                continue
            try:
                files = client.torrents_files(torrent_hash=h)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"刷流直传：读取文件失败 {getattr(t,'name','')}: {e}")
                continue
            vids = [f for f in files
                    if os.path.splitext(getattr(f, "name", ""))[1].lower() in VIDEO_EXT
                    and (getattr(f, "progress", 0) or 0) >= 0.999]
            if len(vids) != 1:
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": f"视频文件数={len(vids)}", "ts": time.time()}
                logger.info(f"刷流直传：跳过 {getattr(t,'name','')}（视频文件数={len(vids)}）")
                self.save_data("state_v2", state)
                continue
            video = {"name": vids[0].name, "size": vids[0].size}
            meta = self._parse_release(video["name"])
            if meta["is_tv"]:
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": "剧集（口径：不收剧集）", "meta": meta, "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → 剧集")
                self.save_data("state_v2", state)
                continue
            tmdb = self._tmdb_search(meta["cn"] or meta["title"], meta["year"], meta["is_tv"])
            if not tmdb:
                state[h] = {"status": "review", "name": getattr(t, "name", ""), "reason": "TMDB 未识别",
                            "meta": meta, "ts": time.time()}
                logger.info(f"刷流直传：TMDB 未识别 {meta['title']} {meta['year']}")
                self.save_data("state_v2", state)
                continue
            chs_meta = {"tmdb_lang": tmdb.get("original_language"),
                        "tmdb_title": tmdb.get("title") or tmdb.get("name")}
            chs_ok, chs_reason = (True, "未检查") if not self._require_chs else self._has_chs(video, files, chs_meta)
            ok, reason = self._judge(meta, video, chs_ok, chs_reason)
            if not ok:
                state[h] = {"status": "review", "name": getattr(t, "name", ""), "reason": reason,
                            "meta": meta, "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → {reason}")
                self.save_data("state_v2", state)
                continue
            year, folder, fname = self._build_names(meta, tmdb, video)
            src = f"{self._src_prefix}/{video['name'].lstrip('/')}"
            dst_dir = f"{self._dest_root}/{folder}"
            rec = {"status": "copying" if not self._dry_run else "planned",
                   "name": getattr(t, "name", ""), "title": tmdb.get("title") or tmdb.get("name"),
                   "year": year, "tmdb": tmdb.get("id"), "src": src, "dst_dir": dst_dir,
                   "orig_name": os.path.basename(video["name"]), "target_name": fname,
                   "size": video["size"], "ts": time.time()}
            if self._dry_run:
                logger.info(f"刷流直传：PLAN {src} → {dst_dir}/{fname}")
                state[h] = rec
                self.save_data("state_v2", state)
                continue
            try:
                self._cd2("CreateFolder", {"parentPath": self._dest_root, "folderName": folder})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"刷流直传：建目录失败（可能已存在）: {e}")
            try:
                self._cd2("CopyFile", {"theFilePaths": [src], "destPath": dst_dir,
                                       "conflictPolicy": "Rename"}, timeout=120)
                rec["copy_started"] = time.time()
                state[h] = rec
                logger.info(f"刷流直传：COPY {src} → {dst_dir}/{fname}")
            except Exception as e:  # noqa: BLE001
                rec["status"] = "error"
                rec["error"] = str(e)[:200]
                state[h] = rec
                logger.warning(f"刷流直传：复制失败 {src}: {e}")
            self.save_data("state_v2", state)

    def _check_copying(self, rec: dict) -> None:
        folder = rec["dst_dir"]
        orig = f"{folder}/{rec['orig_name']}"
        found = self._cd2_find(folder, orig)
        if found:
            target = f"{folder}/{rec['target_name']}"
            if os.path.basename(found.get("fullPathName", "")) != rec["target_name"]:
                try:
                    self._cd2("RenameFile", {"theFilePath": found["fullPathName"],
                                             "newName": rec["target_name"]})
                    logger.info(f"刷流直传：RENAME → {rec['target_name']}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"刷流直传：重命名失败: {e}")
            rec["status"] = "done"
            rec["finished"] = time.time()
            logger.info(f"刷流直传：DONE {target}")
            return
        try:
            tasks = self._cd2("GetCopyTasks").get("copyTasks", [])
        except Exception:
            tasks = []
        for task in tasks:
            if task.get("sourcePath") == rec["src"] and task.get("destPath") == folder:
                if task.get("status") == "Failed":
                    rec["status"] = "error"
                    rec["error"] = task.get("errors")
                    logger.warning(f"刷流直传：复制任务失败 {rec['name']}")
                break
