# -*- coding: utf-8 -*-
"""
刷流直传 115（Brush2Pan）

刷流（BrushFlow）下载完成的影片，按口径识别/筛选后，通过 CD2 gRPC 的 CopyFile
把本地文件直接上传到 115 固定目录，并按 TMDB 名称重命名（MP 风格）。

- 不上传剧集、不收蓝光原盘（BDMV/m2ts/ISO）
- 只收 1080p / 2160p
- 排除 5.1 声道（只排 5.1，7.1/Atmos 等放行）
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
import gzip
import threading
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".wmv", ".flv", ".webm", ".m2ts", ".iso"}
BAD_GENRE_IDS = {27: "恐怖", 53: "惊悚"}
IMDB_DATASET_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
IMDB_MAX_AGE_DAYS = 7
AUDIO_BAD = re.compile(r"(?i)(truehd|dts)")
RETRY_DELAYS = (300, 900, 3600)
MAX_RETRY = 3
COPY_TIMEOUT = 7200
# 规则版本：变更后自动重评历史 review 条目（v2=音轨改为只排 TrueHD/DTS，default 轨判定）
POLICY_VER = 2
# token 化的标记识别：按分隔符切 token，避免 "dd"/"dv" 子串误伤片名（Hidden.Figures → Hi）
TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
MARKER_TOKENS = {
    "2160p", "1080p", "720p", "480p", "4k", "uhd", "hdr", "hdr10", "hdr10plus",
    "dovi", "dv", "webdl", "web", "webrip", "web-dl", "bluray", "blu", "ray",
    "hdtv", "remux", "hevc", "avc", "x264", "x265", "h264", "h265",
    "aac", "ddp", "dd", "atmos", "truehd", "dts", "dtshd", "dtsma", "flac",
    "10bit", "8bit", "repack", "proper", "extended", "unrated", "complete",
    "bd", "bdrip", "brrip", "dvdrip", "hdr10p", "dvhe", "dvhe",
}
AUX_TOKENS = {"sample", "samples", "trailer", "trailers", "featurette",
              "featurettes", "extra", "extras", "bonus", "teaser", "teasers",
              "花絮", "预告", "样片"}
RES_TOKENS = {"2160p", "1080p", "720p", "480p", "4k"}
STRIP_TITLE_TOKENS = {"usa", "cc", "2in1", "hybrid", "remastered", "uncut", "imax",
                      "dubbed", "subbed", "dual", "multi", "2audios", "hc"}
EP_TOKEN_RE = re.compile(r"(?i)^s(\d{2,3})(e\d{1,3})?$")
YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])")
def _tokens(name: str) -> list:
    return [t for t in TOKEN_SPLIT_RE.split(name or "") if t]


def _is_marker_token(tok: str) -> bool:
    t = tok.lower()
    if t in MARKER_TOKENS:
        return True
    if re.match(r"^(x|h)26[45]$", t):
        return True
    if re.match(r"^(ddp?|aac|dts|flac)(\d(?:\.\d)?)?$", t):
        return True
    return False


def _is_aux(name: str) -> bool:
    parts = [p.lower() for p in _tokens(name)]
    if any(p in AUX_TOKENS for p in parts):
        return True
    low = " ".join(parts)
    if "behind the scenes" in low or "behind scenes" in low:
        return True
    return False


def _is_disc(name: str) -> bool:
    low = (name or "").lower()
    if low.endswith(".m2ts") or low.endswith(".iso"):
        return True
    if "/bdmv/" in low or low.startswith("bdmv/") or "bdmv" in [t.lower() for t in _tokens(name)]:
        return True
    return False


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
    plugin_version = "0.3.22"
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
        self._imdb_enabled = bool(cfg.get("imdb_adult_check", True))
        self._imdb_lock = threading.Lock()
        self._probe_cache = {}
        self._tmdb_cache = {}
        if self._imdb_enabled:
            try:
                self._imdb_ensure(background=True)
            except Exception:
                pass
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
            "path": "/ui",
            "endpoint": self.api_ui,
            "methods": ["GET"],
            "summary": "设置详情页分页",
            "description": "设置每页条数与当前页",
        }, {
            "path": "/status",
            "endpoint": self.api_status,
            "methods": ["GET"],
            "summary": "刷流种子状态",
            "description": "返回每个刷流种子的状态与拒绝原因",
        }, {
            "path": "/imdbstatus",
            "endpoint": self.api_imdbstatus,
            "methods": ["GET"],
            "summary": "IMDb 成人过滤状态",
            "description": "返回 IMDb 数据集/索引状态，可配合 /imdbupdate 触发更新",
        }, {
            "path": "/imdbupdate",
            "endpoint": self.api_imdbupdate,
            "methods": ["GET"],
            "summary": "更新 IMDb 数据集",
            "description": "后台下载 title.basics 并重建 Adult 索引（约 221MB）",
        }, {
            "path": "/imdbcheck",
            "endpoint": self.api_imdbcheck,
            "methods": ["GET"],
            "summary": "IMDb 成人检测",
            "description": "传入 tconst（tt...），返回是否 Adult",
        }, {
            "path": "/retry",
            "endpoint": self.api_retry,
            "methods": ["GET"],
            "summary": "重评/重试一条台账",
            "description": "传 name 或 hash，删除该条台账状态，下一轮扫描重新判定（用于规则变更后旧拒绝重评）",
        }, {
            "path": "/audiocheck",
            "endpoint": self.api_audiocheck,
            "methods": ["GET"],
            "summary": "实际音轨检测",
            "description": "传入种子内视频相对路径，用 ffprobe 读实际音频流（default 轨优先），TrueHD/DTS 拒绝；detail=1 返回全部流信息",
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

    def api_imdbstatus(self):
        st = self._imdb_status()
        st["enabled"] = bool(getattr(self, "_imdb_enabled", True))
        return st

    def api_imdbupdate(self, force: int = 1, rebuild: int = 0):
        if int(rebuild or 0):
            if not self._imdb_status()["dataset"]:
                return {"started": False, "error": "数据集不存在，无法只重建索引"}
            t = threading.Thread(target=lambda: self._imdb_update(force=False), daemon=True)
            self._imdb_worker = t
            t.start()
            return {"started": True, "mode": "rebuild_index_only", "status": self._imdb_status()}
        started = self._imdb_ensure(force=bool(int(force or 0)), background=True)
        return {"started": started, "status": self._imdb_status()}

    def api_imdbcheck(self, imdb: str = ""):
        if not imdb.startswith("tt"):
            return {"ok": False, "error": "imdb 需为 tt 开头的 tconst"}
        bad = self._imdb_adult(imdb)
        return {"imdb": imdb, "index_ready": bad is not None, "adult": bad,
                "reason": "索引未就绪" if bad is None else ("成人影片（IMDb Adult）" if bad else "非成人")}

    def api_retry(self, name: str = "", hash: str = ""):
        state = self.get_data("state_v2") or {}
        target = None
        if hash and hash in state:
            target = hash
        elif name:
            for h, rec in state.items():
                if (rec or {}).get("name") == name:
                    target = h
                    break
        if not target:
            return {"ok": False, "error": "未找到对应台账（name/hash）"}
        old = state.pop(target)
        self.save_data("state_v2", state)
        return {"ok": True, "hash": target, "removed_status": (old or {}).get("status"),
                "removed_reason": (old or {}).get("reason")}

    def api_audiocheck(self, name: str = "", detail: int = 0, path: str = ""):
        if path:
            if ".." in path or not path.startswith("/downloads/"):
                return {"ok": False, "error": "path 必须是容器内 /downloads/ 下的绝对路径"}
            target = path
        elif name and ".." not in name:
            target = os.path.join(self._save_prefix, name.lstrip("/"))
        else:
            return {"ok": False, "error": "需要 name（种子内相对路径）或 path（/downloads/ 绝对路径）"}
        video = {"name": target, "size": 0}
        ok, reason = self._audio_check(video)
        out = {"ok": ok, "reason": reason, "path": target}
        if int(detail or 0):
            out["streams"] = self._stream_detail(video)
        return out

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
            try:
                r = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb.get('id')}/external_ids",
                                 params={"api_key": self._tmdb_key}, timeout=20, proxies=self._proxies())
                out["imdb_id"] = (r.json() or {}).get("imdb_id") if r.status_code == 200 else None
            except Exception:
                out["imdb_id"] = None
            if out.get("imdb_id"):
                out["imdb_adult"] = self._imdb_adult(out["imdb_id"])
            gok, greason, gdetail = self._genre_check(tmdb, meta["is_tv"])
            out["genre_ok"] = gok
            out["genre_reason"] = greason
            out["genres"] = [g.get("name") for g in ((gdetail or {}).get("genres") or []) if isinstance(g, dict)]
            out["adult"] = bool((gdetail or {}).get("adult"))
            out["runtime"] = (gdetail or {}).get("runtime")
        if tmdb:
            year, folder, fname = self._build_names(meta, tmdb, {"name": name})
            out["year"], out["folder"], out["target_name"] = year, folder, fname
        return out

    def get_page(self) -> Optional[List[dict]]:
        rows = self._status_rows()
        if rows is None:
            return [{"component": "VAlert", "props": {"type": "warning", "text": "未找到可用的 qbittorrent 下载器"}}]
        ui = self.get_data("ui") or {}
        try:
            size = int(ui.get("size") or 10)
        except Exception:
            size = 10
        if size not in (10, 20, 50, 100):
            size = 10
        rank = {"downloading": 0, "pending": 1, "copying": 2, "review": 3, "planned": 4, "done": 5}
        ordered = sorted(rows, key=lambda r: (rank.get(r["status"], 9), r["name"]))
        total = len(ordered)
        pages = max(1, (total + size - 1) // size)
        done = sum(1 for r in rows if r["status"] == "done")
        review = sum(1 for r in rows if r["status"] == "review")
        other = total - done - review
        esc = lambda x: (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#x27;").replace('"', "&quot;"))
        badge = {
            "done": ("#2e7d32", "rgba(46,125,50,.15)"),
            "review": ("#c62828", "rgba(198,40,40,.15)"),
            "downloading": ("#1565c0", "rgba(21,101,192,.15)"),
            "pending": ("#6a1b9a", "rgba(106,27,154,.15)"),
            "copying": ("#ef6c00", "rgba(239,108,0,.15)"),
            "planned": ("#ef6c00", "rgba(239,108,0,.15)"),
            "error": ("#c62828", "rgba(198,40,40,.15)"),
        }
        status_cn = {"done": "已上传", "review": "拒绝/跳过", "downloading": "下载中",
                     "pending": "待处理", "copying": "复制中", "planned": "演练计划",
                     "retry": "重试中", "waiting": "等待文件", "error": "出错"}
        trs = []
        for idx, r in enumerate(ordered):
            fg, bg = badge.get(r["status"], ("#555", "rgba(128,128,128,.15)"))
            hidden = "" if idx < size else "display:none"
            trs.append(
                f"<tr class='b2p-row' style='border-bottom:1px solid rgba(128,128,128,.12);{hidden}'>"
                f"<td style='padding:9px 10px;white-space:nowrap;color:#888'>{esc(r['size'])}</td>"
                f"<td style='padding:9px 10px;white-space:nowrap;color:#888'>{esc(r['progress'])}</td>"
                f"<td style='padding:9px 10px;white-space:nowrap'>"
                f"<span style='display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;color:{fg};background:{bg}'>{esc(status_cn.get(r['status'], r['status']))}</span></td>"
                f"<td style='padding:9px 10px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' title='{esc(r['name'])}'>{esc(r['name'])}</td>"
                f"<td style='padding:9px 10px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' title='{esc(r['reason'])}'>{esc(r['reason'])}</td></tr>")

        def nav_js(action):
            return ("(function(el,a){var w=el.closest('.b2p-wrap');if(!w)return;"
                    "var rows=w.querySelectorAll('.b2p-row');var size=+w.dataset.size;var page=+w.dataset.page;"
                    "if(a==='next')page++;else if(a==='prev')page--;else if(a.charAt(0)==='s'){size=+a.slice(1);page=1;}"
                    "var pages=Math.max(1,Math.ceil(rows.length/size));"
                    "page=Math.min(Math.max(1,page),pages);w.dataset.page=page;w.dataset.size=size;"
                    "for(var i=0;i<rows.length;i++){rows[i].style.display=(i>=(page-1)*size&&i<page*size)?'':'none';}"
                    "var info=w.querySelector('.b2p-info');if(info)info.textContent='第 '+page+'/'+pages+' 页 · 每页 '+size+' 条';"
                    "})(this,'" + action + "')")

        def btn_html(label, action, extra=""):
            return (f"<button type='button' onclick=\"{nav_js(action)}\" "
                    f"style='margin:0 6px 6px 0;padding:4px 12px;border-radius:6px;border:1px solid rgba(128,128,128,.35);"
                    f"background:rgba(128,128,128,.08);color:inherit;font-size:13px;cursor:pointer;{extra}'>{label}</button>")

        size_btns = "".join(btn_html(f"{sz} 条", f"s{sz}") for sz in (10, 20, 50, 100))
        html = (
            "<style>"
            ".b2p-wrap{padding:2px 2px 8px}"
            ".b2p-chips{display:flex;gap:8px;flex-wrap:wrap;margin:2px 0 10px}"
            ".b2p-chip{padding:2px 10px;border-radius:999px;font-size:12px;border:1px solid rgba(128,128,128,.3)}"
            ".b2p-scroll{max-height:420px;overflow:auto;border:1px solid rgba(128,128,128,.22);border-radius:10px}"
            ".b2p-tbl{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}"
            ".b2p-tbl thead th{position:sticky;top:0;background:rgba(128,128,128,.10);backdrop-filter:blur(6px);"
            "text-align:left;padding:8px 10px;font-weight:600;color:#888;border-bottom:1px solid rgba(128,128,128,.22);z-index:1}"
            ".b2p-tbl tbody tr:hover{background:rgba(128,128,128,.08)}"
            "</style>"
            f"<div class='b2p-wrap' data-page='1' data-size='{size}'>"
            "<div class='b2p-chips'>"
            f"<span class='b2p-chip' style='color:#2e7d32'>已上传 {done}</span>"
            f"<span class='b2p-chip' style='color:#c62828'>拒绝/跳过 {review}</span>"
            f"<span class='b2p-chip' style='color:#1565c0'>下载中/待处理 {other}</span>"
            f"<span class='b2p-chip' style='color:#888'>共 {total} 个种子</span>"
            f"<span class='b2p-chip b2p-info' style='color:#888'>第 1/{pages} 页 · 每页 {size} 条</span>"
            "</div>"
            "<div class='b2p-scroll'><table class='b2p-tbl'>"
            "<thead><tr><th>体积</th><th>进度</th><th>状态</th><th>种子</th><th>原因 / 目标</th></tr></thead>"
            "<tbody>" + "".join(trs) + "</tbody></table></div>"
            "<div style='margin-top:10px'>"
            + btn_html("上一页", "prev") + btn_html("下一页", "next")
            + "<span style='display:inline-block;margin:0 8px'>每页：</span>" + size_btns
            + "</div></div>"
        )
        return [{"component": "div", "html": html}]

    def _status_rows(self):
        client = self._qb_client()
        if not client:
            return None
        try:
            torrents = client.torrents_info()
        except Exception:
            return None
        state = self.get_data("state_v2") or {}
        rows = []
        for t in torrents:
            save_path = getattr(t, "save_path", "") or ""
            if not save_path.startswith(self._save_prefix):
                continue
            h = getattr(t, "hash", "")
            rec = state.get(h) or {}
            progress = getattr(t, "progress", 0) or 0
            if rec.get("status") == "done":
                status, reason = "done", rec.get("target_name") or "已上传"
            elif rec.get("status") == "copying":
                status, reason = "copying", "复制中"
            elif rec.get("status") == "review":
                status, reason = "review", rec.get("reason") or "被拒绝"
            elif rec.get("status") == "planned":
                status, reason = "planned", rec.get("target_name") or "演练计划"
            elif rec.get("status") == "retry":
                status, reason = "retry", rec.get("reason") or "重试中"
            elif rec.get("status") == "waiting":
                status, reason = "waiting", rec.get("reason") or "等待文件就绪"
            elif rec.get("status") == "error":
                status, reason = "error", rec.get("reason") or rec.get("error") or "出错"
            else:
                status = "downloading" if progress < 0.999 else "pending"
                reason = f"下载中 {progress*100:.1f}%" if progress < 0.999 else "等待下一轮扫描"
            rows.append({
                "status_cn": {"done": "已上传", "review": "拒绝/跳过", "downloading": "下载中",
                              "pending": "待处理", "copying": "复制中", "planned": "演练计划",
                              "retry": "重试中", "waiting": "等待文件", "error": "出错"}.get(status, status),
                "name": getattr(t, "name", ""),
                "size": f"{(getattr(t,'size',0) or 0)/2**30:.2f}GB",
                "progress": f"{progress*100:.1f}%",
                "status": status,
                "reason": reason,
            })
        rows.sort(key=lambda r: r["name"])
        return rows

    def api_ui(self, page: int = 1, size: int = 10):
        try:
            size = int(size)
        except Exception:
            size = 10
        if size not in (10, 20, 50, 100):
            size = 10
        try:
            page = max(1, int(page))
        except Exception:
            page = 1
        self.save_data("ui", {"page": page, "size": size})
        return {"ok": True, "page": page, "size": size}

    def api_status(self):
        rows = self._status_rows()
        return {"ok": rows is not None, "rows": rows or []}

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
                                "model": "imdb_adult_check", "label": "IMDb 成人过滤",
                                "hint": "用 IMDb 数据集 Adult 类型/成人标记判断色情片，首次启用需下载约 221MB 数据集。",
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
            "imdb_adult_check": True,
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

    def _cd2_checked(self, method: str, payload: Optional[dict] = None, timeout: int = 60) -> dict:
        """调用 CD2 写操作并校验 FileOperationResult.success；失败抛异常（调用方按临时错误重试）。"""
        data = self._cd2(method, payload, timeout=timeout)
        if isinstance(data, dict):
            if "success" in data and data.get("success") is False:
                raise RuntimeError(f"{method} 失败: {data.get('errorMessage') or data}")
            if method == "CreateFolder" and not data.get("folderCreated"):
                raise RuntimeError("CreateFolder 未返回 folderCreated")
        return data

    def _cd2_find(self, parent: str, path: str) -> Optional[dict]:
        """查文件；NotFound 返回 None，其它错误（token/网络/协议）抛异常，调用方必须 fail-closed。"""
        try:
            return self._cd2("FindFileByPath", {"parentPath": parent, "path": path})
        except RuntimeError as e:
            if "NotFound" in str(e) or "not found" in str(e).lower():
                return None
            raise

    @staticmethod
    def _size_of(found: dict) -> int:
        try:
            return int((found or {}).get("size") or 0)
        except Exception:
            return 0

    # ---------------- 识别 / 判定 ----------------
    @staticmethod
    def _parse_release(filename: str) -> dict:
        stem = os.path.splitext(os.path.basename(filename))[0]
        cn = None
        m = re.match(r"^\[([^\]]+)\]", stem)
        body = stem
        if m:
            cn = m.group(1).strip()
            body = stem[m.end():]
        toks = _tokens(body)
        res = None
        for t in toks:
            lt = t.lower()
            if lt in RES_TOKENS:
                res = "2160p" if lt == "4k" else lt
                break
        years = [int(t) for t in toks if re.fullmatch(r"(?:19|20)\d{2}", t)]
        year = years[-1] if years else None
        cut_idx = len(toks)
        for i, t in enumerate(toks):
            if _is_marker_token(t) or EP_TOKEN_RE.match(t):
                cut_idx = i
                break
        title_toks = toks[:cut_idx]
        # 去掉发布属性词与与发布年相同的年份 token（年份出现在中间也能剔除，如 The Shining 1980 USA CC）
        title_toks = [t for t in title_toks
                      if t.lower() not in STRIP_TITLE_TOKENS
                      and not (re.fullmatch(r"(?:19|20)\d{2}", t) and int(t) == (year if year is not None else -1))]
        if not title_toks:
            title_toks = [t for t in toks if not _is_marker_token(t) and not EP_TOKEN_RE.match(t)]
        title = " ".join(title_toks).strip(" -_")
        if not title:
            title = body.strip()
        tv = any(EP_TOKEN_RE.match(t) for t in toks) or bool(re.search(r"(?i)S\d{2,3}(E\d{1,3})?", body))
        return {"title": cn or title, "year": year, "resolution": res, "is_tv": tv, "cn": cn}

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
        key = (title, year, bool(is_tv))
        cache = getattr(self, "_tmdb_cache", None)
        if cache is None:
            cache = self._tmdb_cache = {}
        if key in cache:
            return cache[key]
        for variant in self._title_variants(title) or [title]:
            result = self._tmdb_search_one(variant, year, is_tv)
            if result:
                cache[key] = result
                return result
        return None

    def _tmdb_detail(self, tmdb_id, is_tv: bool = False) -> Optional[dict]:
        if not self._tmdb_key or not tmdb_id:
            return None
        path = ("tv/" if is_tv else "movie/") + str(tmdb_id)
        try:
            r = requests.get("https://api.themoviedb.org/3/" + path,
                             params={"api_key": self._tmdb_key, "language": "zh-CN"},
                             timeout=20, proxies=self._proxies())
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    # ---------- IMDb 成人（色情）过滤 ----------
    def _imdb_dir(self):
        return self.get_data_path() / "imdb"

    def _imdb_status(self) -> dict:
        d = self._imdb_dir()
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        ds = d / "title.basics.tsv.gz"
        worker = getattr(self, "_imdb_worker", None)
        return {"dir": str(d), "dataset": ds.exists(),
                "dataset_mb": round(ds.stat().st_size / 1024 / 1024, 1) if ds.exists() else 0,
                "index": (d / "adult.tsv").exists(), "updated": meta.get("updated"),
                "count": meta.get("count"), "titles": meta.get("titles"),
                "updating": bool(worker and worker.is_alive())}

    def _imdb_ensure(self, force: bool = False, background: bool = True) -> bool:
        st = self._imdb_status()
        age = time.time() - float(st["updated"]) if st.get("updated") else 1e12
        need = force or (not st["index"]) or age > IMDB_MAX_AGE_DAYS * 86400
        if not need or st["updating"]:
            return False
        if background:
            def job():
                try:
                    self._imdb_update(force=force)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"刷流直传：IMDb 数据集更新失败 {e}")
            t = threading.Thread(target=job, daemon=True)
            self._imdb_worker = t
            t.start()
            logger.info("刷流直传：IMDb 数据集更新已在后台启动")
            return True
        self._imdb_update()
        return True

    def _imdb_download(self, ds, tmp) -> None:
        logger.info("刷流直传：开始下载 IMDb 数据集（约 221MB）")
        with requests.get(IMDB_DATASET_URL, stream=True, timeout=(20, 180), proxies=self._proxies()) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            mark = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if got - mark >= 30 * 1024 * 1024:
                        mark = got
                        logger.info(f"刷流直传：IMDb 数据集 {got/1024/1024:.0f}/{total/1024/1024:.0f}MB")
        tmp.replace(ds)
        logger.info("刷流直传：IMDb 数据集下载完成")

    def _imdb_update(self, force: bool = False) -> None:
        if not self._imdb_lock.acquire(blocking=False):
            logger.info("刷流直传：IMDb 更新已在进行，跳过重复任务")
            return
        try:
            self._imdb_update_locked(force=force)
        finally:
            self._imdb_lock.release()

    def _imdb_update_locked(self, force: bool = False) -> None:
        d = self._imdb_dir()
        d.mkdir(parents=True, exist_ok=True)
        ds = d / "title.basics.tsv.gz"
        tmp = d / "title.basics.tsv.part"
        fresh = ds.exists() and (time.time() - ds.stat().st_mtime) < IMDB_MAX_AGE_DAYS * 86400
        if fresh and not force:
            logger.info("刷流直传：IMDb 数据集仍在有效期内，跳过下载，仅重建索引")
        else:
            self._imdb_download(ds, tmp)
        logger.info("刷流直传：构建 IMDb Adult 索引")
        adult = set()
        n = 0
        with gzip.open(ds, "rt", encoding="utf-8", errors="ignore") as f:
            f.readline()
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 9:
                    continue
                n += 1
                if parts[4] == "1" or "Adult" in parts[8].split(","):
                    adult.add(parts[0])
        with open(d / "adult.tsv", "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(adult)))
        (d / "meta.json").write_text(json.dumps(
            {"updated": time.time(), "count": len(adult), "titles": n,
             "dataset_bytes": ds.stat().st_size}, ensure_ascii=False), encoding="utf-8")
        self._imdb_adult_cache = adult
        logger.info(f"刷流直传：IMDb 索引完成 Adult={len(adult)} / titles={n}")

    def _imdb_set(self):
        if not self._imdb_status()["index"]:
            return None
        cached = getattr(self, "_imdb_adult_cache", None)
        if cached is None:
            try:
                cached = set((self._imdb_dir() / "adult.tsv").read_text(encoding="utf-8").split())
            except Exception:
                return None
            self._imdb_adult_cache = cached
        return cached

    def _imdb_adult(self, imdb_id):
        if not imdb_id:
            return None
        st = self._imdb_set()
        return None if st is None else (imdb_id in st)

    def _imdb_adult_check(self, tmdb: dict):
        """用 IMDb 数据集的 Adult 类型/isAdult 标记判断色情片。
        返回 (reject, reason, kind)；kind: adult/ok/disabled/no_id/not_ready/error"""
        if not getattr(self, "_imdb_enabled", True):
            return None, "IMDb 过滤未启用", "disabled"
        imdb_id = None
        try:
            r = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb.get('id')}/external_ids",
                             params={"api_key": self._tmdb_key}, timeout=20, proxies=self._proxies())
            if r.status_code == 200:
                imdb_id = (r.json() or {}).get("imdb_id")
            else:
                return None, f"IMDb 查询失败 HTTP {r.status_code}", "error"
        except Exception as e:  # noqa: BLE001
            return None, f"IMDb 查询失败: {e}", "error"
        if not imdb_id:
            return None, "无 IMDb ID", "no_id"
        st = self._imdb_status()
        if not st["index"]:
            self._imdb_ensure(background=True)
            return None, "IMDb 索引未就绪（后台构建中）", "not_ready"
        adult_set = self._imdb_set()
        if adult_set is None:
            return None, "IMDb 索引读取失败", "error"
        bad = imdb_id in adult_set
        if bad:
            return True, f"成人影片（IMDb Adult {imdb_id}，口径：排除）", "adult"
        return False, f"IMDb {imdb_id} 非成人", "ok"

    def _genre_check(self, tmdb: dict, is_tv: bool = False):
        """题材/分级口径：排除恐怖(27)、惊悚(53)；类型为空视为未知直接拒；adult=true 拒。"""
        detail = self._tmdb_detail(tmdb.get("id"), is_tv)
        if detail is None:
            return False, "题材未知（TMDB 详情获取失败）", {}
        if detail.get("adult"):
            return False, "TMDB 成人标记（疑似色情，口径：排除）", detail
        genres = detail.get("genres") or []
        ids = [g.get("id") for g in genres if isinstance(g, dict)]
        names = [g.get("name") for g in genres if isinstance(g, dict) and g.get("name")]
        if not ids:
            return False, "题材未知（TMDB 无类型，口径：排除）", detail
        bad = [BAD_GENRE_IDS[i] for i in ids if i in BAD_GENRE_IDS]
        if bad:
            return False, f"题材排除（{'/'.join(bad)}）", detail
        return True, "题材通过（" + "/".join(names) + "）", detail

    @staticmethod
    def _norm_title(x: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (x or "").lower())

    def _tmdb_search_one(self, title: str, year: Optional[int], is_tv: bool) -> Optional[dict]:
        self._tmdb_transient = False
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
            want = self._norm_title(title)
            best = None
            for item in results:
                name = item.get("title") or item.get("name") or ""
                orig = item.get("original_title") or item.get("original_name") or ""
                date = item.get("release_date") or item.get("first_air_date") or ""
                ry = int(date[:4]) if date[:4].isdigit() else None
                exact = self._norm_title(name) == want or (orig and self._norm_title(orig) == want)
                score = 5 if exact else 0
                if year and ry == year:
                    score += 5
                elif year and ry and abs(ry - year) <= 1:
                    score += 2
                if exact and (best is None or score > best[0]):
                    best = (score, item)
            return best[1] if best else None
        except Exception as e:  # noqa: BLE001
            self._tmdb_transient = True
            logger.warning(f"刷流直传：TMDB 查询失败 {title}: {e}")
            return None

    SUB_EXT = {".srt", ".ass", ".ssa", ".sup", ".sub", ".idx"}

    @staticmethod
    def _cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    def _has_chs_cached(self, video: dict, files: List[Any], meta: Optional[dict] = None) -> Tuple[bool, str]:
        path = self._video_path(video)
        cache = getattr(self, "_chs_cache", None)
        if cache is None:
            cache = self._chs_cache = {}
        if path in cache:
            return cache[path]
        result = self._has_chs(video, files, meta)
        if "执行失败" not in result[1] and "检测失败" not in result[1]:
            cache[path] = result
        return result

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

    def _video_path(self, video: dict) -> str:
        name = video.get("name") or ""
        if os.path.isabs(name):
            return name
        return os.path.join(self._save_prefix, name.lstrip("/"))

    def _stream_detail(self, video: dict) -> dict:
        path = self._video_path(video)
        ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
        out = {"audio": [], "video": []}
        try:
            r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json",
                                "-show_format", "-show_streams", path],
                               capture_output=True, text=True, timeout=60)
            raw = json.loads(r.stdout or "{}")
            try:
                out["duration"] = float((raw.get("format") or {}).get("duration") or 0) or None
            except Exception:
                out["duration"] = None
            for st in raw.get("streams", []):
                info = {k: st.get(k) for k in ("index", "codec_name", "profile", "channels",
                                                "channel_layout", "sample_rate", "bit_rate",
                                                "pix_fmt", "color_transfer", "color_primaries", "width", "height")
                        if st.get(k) is not None}
                info["default"] = bool(((st.get("disposition") or {}).get("default")) or 0)
                if st.get("codec_type") == "audio":
                    out["audio"].append(info)
                elif st.get("codec_type") == "video":
                    out["video"].append(info)
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out

    def _audio_check(self, video: dict) -> Tuple[bool, str]:
        """带缓存的音轨检测（同一文件只探测一次，失败不缓存）。"""
        path = self._video_path(video)
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        if path in cache:
            return cache[path]
        result = self._audio_check_raw(video)
        if "检测失败" not in result[1]:
            cache[path] = result
        return result

    def _audio_check_raw(self, video: dict) -> Tuple[bool, str]:
        """读实际音频流（default 轨优先）：TrueHD / DTS 拒绝。"""
        path = self._video_path(video)
        ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
                 "-select_streams", "a", path],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return False, f"音轨检测失败（ffprobe rc={r.returncode}）"
            streams = json.loads(r.stdout or "{}").get("streams", [])
        except Exception as e:  # noqa: BLE001
            return False, f"音轨检测失败: {e}"
        if not streams:
            return False, "未发现音频流"
        # 优先按播放器实际会选的 default 轨判断；没有 default 标记才退回第一条
        default_streams = [s for s in streams if ((s.get("disposition") or {}).get("default"))]
        st = default_streams[0] if default_streams else streams[0]
        try:
            channels = int(st.get("channels") or 0)
        except Exception:
            channels = 0
        codec = str(st.get("codec_name") or "?")
        profile = str(st.get("profile") or "")
        tag = f"{codec} {channels}ch" if channels else codec
        codec_l = codec.lower()
        profile_l = profile.lower()
        if codec_l == "truehd" or "truehd" in profile_l:
            return False, f"实际音轨 TrueHD（{profile or codec}，播放兼容性差，口径：排除）"
        if codec_l.startswith("dts") or codec_l == "dca":
            return False, f"实际音轨 DTS（{profile or codec}，播放兼容性差，口径：排除）"
        if channels == 0:
            return False, f"音轨声道未知（{codec}）"
        return True, f"音轨 {tag}（通过）"

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
            return False, "TrueHD/DTS 音轨（播放兼容性口径：排除）"
        if size_mb < self._min_size_mb:
            return False, f"文件过小 {size_mb:.0f}MB < {self._min_size_mb}MB"
        if "sample" in nl:
            return False, "sample"
        if self._require_chs and not chs_ok:
            return False, f"无中字（{chs_reason}）"
        return True, "通过（中字✓）"

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

    def _select_video(self, files):
        """从文件列表里挑主视频：剔除样片/花絮/小文件，识别剧集/多碟/原盘。
        返回 (video_attr, code, count)；code: ok / no_files / tv / multi / aux_only / disc
        """
        vids = [f for f in files
                if os.path.splitext(getattr(f, "name", ""))[1].lower() in VIDEO_EXT
                and (getattr(f, "progress", 0) or 0) >= 0.999]
        if not vids:
            return None, "no_files", 0
        if all(_is_disc(getattr(f, "name", "") or "") for f in vids):
            return None, "disc", len(vids)
        vids = [f for f in vids if not _is_disc(getattr(f, "name", "") or "")]
        if not vids:
            return None, "no_files", 0
        main = max(vids, key=lambda f: getattr(f, "size", 0) or 0)
        main_size = getattr(main, "size", 0) or 0
        aux = []
        rest = []
        for f in vids:
            if f is main:
                continue
            size = getattr(f, "size", 0) or 0
            if _is_aux(getattr(f, "name", "") or "") or (main_size and size < main_size * 0.05):
                aux.append(f)
            else:
                rest.append(f)
        if _is_aux(getattr(main, "name", "") or "") and not rest:
            return None, "aux_only", len(vids)
        if EP_TOKEN_RE.search(os.path.basename(main.name)) or re.search(r"(?i)S\d{2,3}(E\d{1,3})?", main.name):
            return None, "tv", len(vids) - len(aux)
        if rest:
            return None, "multi", len(vids) - len(aux)
        return main, "ok", len(aux)

    def _media_duration(self, video: dict) -> Optional[float]:
        path = os.path.join(self._save_prefix, (video.get("name") or "").lstrip("/"))
        ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
        try:
            r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return None
            dur = float(json.loads(r.stdout or "{}").get("format", {}).get("duration") or 0)
            return dur or None
        except Exception:
            return None

    def _schedule_retry(self, state: dict, h: str, name: str, reason: str) -> str:
        rec = state.get(h) or {}
        attempts = int(rec.get("attempts") or getattr(self, "_retry_attempts", 0) or 0) + 1
        if attempts > MAX_RETRY:
            state[h] = {"status": "review", "name": name,
                        "reason": f"重试失败：{reason}", "attempts": attempts, "ts": time.time()}
            logger.info(f"刷流直传：REJECT {name} → 重试失败：{reason}")
            return "review"
        delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
        state[h] = {"status": "retry", "name": name, "reason": reason,
                    "attempts": attempts, "next_ts": time.time() + delay, "ts": time.time()}
        logger.info(f"刷流直传：RETRY {name} → {reason}（第{attempts}次，{delay}s 后）")
        return "retry"

    def _copy_task_of(self, src: str, dst: str) -> Optional[dict]:
        try:
            tasks = self._cd2("GetCopyTasks").get("copyTasks", []) or []
        except Exception:
            return None
        for task in tasks:
            if task.get("sourcePath") == src and task.get("destPath") == dst:
                return task
        return None

    def _migrate_policy(self, state: dict) -> None:
        """规则版本升级后，清掉历史 review 记录，让它们在下一轮按新规则重评一次。"""
        try:
            stored = int(self.get_data("policy_ver") or 0)
        except Exception:
            stored = 0
        if stored >= POLICY_VER:
            return
        removed = 0
        for h, rec in list(state.items()):
            if (rec or {}).get("status") == "review":
                state.pop(h, None)
                removed += 1
        if removed:
            self.save_data("state_v2", state)
        self.save_data("policy_ver", POLICY_VER)
        logger.info(f"刷流直传：规则版本升级 v{stored}→v{POLICY_VER}，重评历史拒绝 {removed} 条")

    def _prune_state(self, state: dict, live_hashes: set) -> None:
        """清理已不在 QB 里的历史台账（BrushFlow 删种后），防 state_v2 无限增长。"""
        pruned = 0
        now = time.time()
        for h, rec in list(state.items()):
            if h in live_hashes:
                continue
            if (rec or {}).get("status") == "copying":
                continue
            if now - float((rec or {}).get("ts") or 0) < 86400:
                continue
            state.pop(h, None)
            pruned += 1
        if pruned:
            logger.info(f"刷流直传：清理已删除种子的历史台账 {pruned} 条")

    def scan(self) -> None:
        if not self._enabled:
            return
        client = self._qb_client()
        if not client:
            logger.warning("刷流直传：没有可用的 qbittorrent 下载器")
            return
        state: dict = self.get_data("state_v2") or {}
        self._migrate_policy(state)
        try:
            torrents = client.torrents_info()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷流直传：列举种子失败: {e}")
            return
        self._prune_state(state, {getattr(t, "hash", "") for t in torrents})
        started = time.time()
        for t in torrents:
            if time.time() - started > 50:
                logger.info("刷流直传：本轮扫描超过 50 秒，剩余种子下一轮继续")
                break
            try:
                self._process_torrent(t, state, client)
            except Exception as e:  # noqa: BLE001
                h = getattr(t, "hash", "")
                logger.warning(f"刷流直传：处理种子异常 {getattr(t,'name','')}: {e}")
                if h:
                    self._schedule_retry(state, h, getattr(t, "name", ""), f"处理异常: {e}")
                    self.save_data("state_v2", state)
        self.save_data("state_v2", state)

    def _process_torrent(self, t, state: dict, client) -> None:
        save_path = getattr(t, "save_path", "") or ""
        if not save_path.startswith(self._save_prefix):
            return
        h = getattr(t, "hash", "")
        name = getattr(t, "name", "")
        rec = state.get(h)
        self._retry_attempts = int((rec or {}).get("attempts") or 0)
        if rec and rec.get("status") == "review" and str(rec.get("reason", "")).startswith("视频文件数="):
            state.pop(h, None)
            rec = None
        if rec:
            st = rec.get("status")
            if st in ("done", "review"):
                return
            if st == "copying":
                result, reason = self._check_copying(rec)
                if result == "done":
                    self.save_data("state_v2", state)
                    return
                if result == "error":
                    self._schedule_retry(state, h, name, reason)
                elif time.time() - float(rec.get("copy_started") or rec.get("ts") or 0) > COPY_TIMEOUT:
                    self._schedule_retry(state, h, name, "复制超时（2 小时）")
                self.save_data("state_v2", state)
                return
            if st == "retry":
                if time.time() < float(rec.get("next_ts") or 0):
                    return
                state.pop(h, None)
                rec = None
            elif st == "waiting":
                # 继续往下走：若文件已就绪则重新处理
                pass
            elif st == "error":
                state.pop(h, None)
                rec = None
            elif st == "planned" and self._dry_run:
                return
        if (getattr(t, "progress", 0) or 0) < 0.999:
            return
        try:
            files = client.torrents_files(torrent_hash=h)
        except Exception as e:  # noqa: BLE001
            self._schedule_retry(state, h, name, f"读取文件列表失败: {e}")
            self.save_data("state_v2", state)
            return
        picked, code, ignored = self._select_video(files)
        if code == "no_files":
            attempts = int((state.get(h) or {}).get("attempts") or 0) + 1
            if attempts > 10:
                state[h] = {"status": "review", "name": name, "reason": "文件未就绪超时（10 次）",
                            "attempts": attempts, "ts": time.time()}
                logger.info(f"刷流直传：REJECT {name} → 文件未就绪超时")
            else:
                state[h] = {"status": "waiting", "name": name, "attempts": attempts, "ts": time.time()}
                if attempts <= 2 or attempts in (5, 10):
                    logger.info(f"刷流直传：文件未就绪，等待（第{attempts}次） {name}")
            self.save_data("state_v2", state)
            return
        if code == "disc":
            state[h] = {"status": "review", "name": name, "reason": "蓝光原盘（口径：不收）", "ts": time.time()}
            logger.info(f"刷流直传：REJECT {name} → 蓝光原盘")
            self.save_data("state_v2", state)
            return
        if code == "aux_only":
            state[h] = {"status": "review", "name": name, "reason": "仅样片/花絮，无正片", "ts": time.time()}
            self.save_data("state_v2", state)
            return
        if code == "tv":
            state[h] = {"status": "review", "name": name, "reason": "剧集（口径：不收剧集）", "ts": time.time()}
            logger.info(f"刷流直传：REJECT {name} → 剧集")
            self.save_data("state_v2", state)
            return
        if code == "multi":
            state[h] = {"status": "review", "name": name,
                        "reason": f"视频文件数={ignored}（疑似剧集/合集）", "ts": time.time()}
            self.save_data("state_v2", state)
            return
        video = {"name": picked.name, "size": picked.size}
        meta = self._parse_release(video["name"])
        if meta["is_tv"]:
            state[h] = {"status": "review", "name": name, "reason": "剧集（口径：不收剧集）",
                        "meta": meta, "ts": time.time()}
            self.save_data("state_v2", state)
            return
        tmdb = self._tmdb_search(meta["cn"] or meta["title"], meta["year"], meta["is_tv"])
        if not tmdb:
            if getattr(self, "_tmdb_transient", False):
                self._schedule_retry(state, h, name, "TMDB 查询失败（网络/接口）")
            else:
                state[h] = {"status": "review", "name": name, "reason": "TMDB 未识别",
                            "meta": meta, "ts": time.time()}
                logger.info(f"刷流直传：TMDB 未识别 {meta['title']} {meta['year']}")
            self.save_data("state_v2", state)
            return
        genre_ok, genre_reason, genre_detail = self._genre_check(tmdb, meta["is_tv"])
        if not genre_ok:
            if "获取失败" in genre_reason:
                self._schedule_retry(state, h, name, genre_reason)
            else:
                state[h] = {"status": "review", "name": name, "reason": genre_reason,
                            "meta": meta, "tmdb": tmdb.get("id"),
                            "genres": [g.get("name") for g in ((genre_detail or {}).get("genres") or []) if isinstance(g, dict)],
                            "ts": time.time()}
                logger.info(f"刷流直传：REJECT {name} → {genre_reason}")
            self.save_data("state_v2", state)
            return
        imdb_bad, imdb_reason, imdb_kind = self._imdb_adult_check(tmdb)
        if imdb_bad:
            state[h] = {"status": "review", "name": name, "reason": imdb_reason,
                        "meta": meta, "tmdb": tmdb.get("id"), "ts": time.time()}
            logger.info(f"刷流直传：REJECT {name} → {imdb_reason}")
            self.save_data("state_v2", state)
            return
        if imdb_kind in ("not_ready", "error"):
            self._schedule_retry(state, h, name, imdb_reason)
            self.save_data("state_v2", state)
            return
        # TMDB 匹配校验：实际时长 vs TMDB runtime（电影）
        tmdb_runtime = (genre_detail or {}).get("runtime")
        duration = self._media_duration(video)
        if tmdb_runtime and duration:
            diff_min = abs(duration / 60.0 - float(tmdb_runtime))
            if diff_min > 5:
                state[h] = {"status": "review", "name": name,
                            "reason": f"TMDB 匹配存疑（时长 {duration/60:.0f} 分钟 vs {tmdb_runtime} 分钟）",
                            "meta": meta, "tmdb": tmdb.get("id"), "ts": time.time()}
                logger.info(f"刷流直传：REJECT {name} → TMDB 匹配存疑（时长）")
                self.save_data("state_v2", state)
                return
        chs_meta = {"tmdb_lang": tmdb.get("original_language"),
                    "tmdb_title": tmdb.get("title") or tmdb.get("name")}
        chs_ok, chs_reason = (True, "未检查") if not self._require_chs else self._has_chs_cached(video, files, chs_meta)
        if self._require_chs and not chs_ok and "执行失败" in chs_reason:
            self._schedule_retry(state, h, name, f"中字检测失败: {chs_reason}")
            self.save_data("state_v2", state)
            return
        audio_ok, audio_reason = self._audio_check(video)
        if not audio_ok:
            if "检测失败" in audio_reason or "未发现音频流" in audio_reason:
                self._schedule_retry(state, h, name, audio_reason)
            else:
                state[h] = {"status": "review", "name": name, "reason": audio_reason,
                            "meta": meta, "tmdb": tmdb.get("id"), "ts": time.time()}
                logger.info(f"刷流直传：REJECT {name} → {audio_reason}")
            self.save_data("state_v2", state)
            return
        ok, reason = self._judge(meta, video, chs_ok, chs_reason)
        if not ok:
            state[h] = {"status": "review", "name": name, "reason": reason, "meta": meta, "ts": time.time()}
            logger.info(f"刷流直传：REJECT {name} → {reason}")
            self.save_data("state_v2", state)
            return
        year, folder, fname = self._build_names(meta, tmdb, video)
        src = f"{self._src_prefix}/{video['name'].lstrip('/')}"
        dst_dir = f"{self._dest_root}/{folder}"
        rec = {"status": "copying" if not self._dry_run else "planned",
               "attempts": getattr(self, "_retry_attempts", 0),
               "name": name, "title": tmdb.get("title") or tmdb.get("name"),
               "year": year, "tmdb": tmdb.get("id"), "src": src, "dst_dir": dst_dir,
               "orig_name": os.path.basename(video["name"]), "target_name": fname,
               "size": video["size"], "audio": audio_reason, "ts": time.time()}
        if self._dry_run:
            logger.info(f"刷流直传：PLAN {src} → {dst_dir}/{fname}")
            state[h] = rec
            self.save_data("state_v2", state)
            return
        # 去重：目标已存在（校验大小）→ done；目标目录有残留原名文件 → 进入复制检查/重命名
        try:
            found_t = self._cd2_find(dst_dir, f"{dst_dir}/{fname}")
        except Exception as e:  # noqa: BLE001
            self._schedule_retry(state, h, name, f"CD2 去重查询失败: {e}")
            self.save_data("state_v2", state)
            return
        if found_t:
            fsize = self._size_of(found_t)
            if rec["size"] and fsize and int(rec["size"]) != fsize:
                self._schedule_retry(state, h, name, f"目标已存在但大小不一致（{fsize} vs {int(rec['size'])}）")
                self.save_data("state_v2", state)
                return
            rec["status"] = "done"
            rec["finished"] = time.time()
            state[h] = rec
            logger.info(f"刷流直传：目标已存在，跳过 {dst_dir}/{fname}")
            self.save_data("state_v2", state)
            return
        try:
            found_o = self._cd2_find(dst_dir, f"{dst_dir}/{rec['orig_name']}")
        except Exception as e:  # noqa: BLE001
            self._schedule_retry(state, h, name, f"CD2 查询残留文件失败: {e}")
            self.save_data("state_v2", state)
            return
        if found_o:
            rec["copy_started"] = time.time()
            state[h] = rec
            logger.info(f"刷流直传：发现残留原文件，进入重命名流程 {dst_dir}/{rec['orig_name']}")
            self.save_data("state_v2", state)
            return
        try:
            self._cd2_checked("CreateFolder", {"parentPath": self._dest_root, "folderName": folder})
        except Exception as e:  # noqa: BLE001
            self._schedule_retry(state, h, name, f"建目录失败: {e}")
            self.save_data("state_v2", state)
            return
        existing = self._copy_task_of(src, dst_dir)
        if existing and existing.get("status") != "Failed":
            rec["copy_started"] = time.time()
            state[h] = rec
            logger.info(f"刷流直传：已存在复制任务（{existing.get('status')}），等待完成")
            self.save_data("state_v2", state)
            return
        rec["copy_started"] = time.time()
        state[h] = rec
        self.save_data("state_v2", state)  # 先落盘，避免 CopyFile 后崩溃导致重复
        try:
            self._cd2_checked("CopyFile", {"theFilePaths": [src], "destPath": dst_dir,
                                           "conflictPolicy": "Rename"}, timeout=120)
            logger.info(f"刷流直传：COPY {src} → {dst_dir}/{fname}")
        except Exception as e:  # noqa: BLE001
            self._schedule_retry(state, h, name, f"复制失败: {e}")
        self.save_data("state_v2", state)

    def _check_copying(self, rec: dict):
        """返回 (result, reason)；result: done / copying / error。"""
        folder = rec["dst_dir"]
        target_name = rec["target_name"]
        try:
            found_t = self._cd2_find(folder, f"{folder}/{target_name}")
        except Exception:
            return "copying", "CD2 查询失败，稍后重试"
        if found_t:
            fsize = self._size_of(found_t)
            rec_size = int(rec.get("size") or 0)
            if rec_size and fsize and fsize != rec_size:
                return "error", f"目标文件大小不一致（{fsize} vs {rec_size}）"
            rec["status"] = "done"
            rec["finished"] = time.time()
            logger.info(f"刷流直传：DONE {folder}/{target_name}")
            return "done", "已上传"
        orig = f"{folder}/{rec['orig_name']}"
        try:
            found_o = self._cd2_find(folder, orig)
        except Exception:
            return "copying", "CD2 查询失败，稍后重试"
        if found_o:
            if os.path.basename(found_o.get("fullPathName", "")) != target_name:
                try:
                    self._cd2_checked("RenameFile", {"theFilePath": found_o["fullPathName"],
                                                     "newName": target_name})
                    logger.info(f"刷流直传：RENAME → {target_name}")
                except Exception as e:  # noqa: BLE001
                    return "copying", f"重命名失败，稍后重试: {e}"
            rec["status"] = "done"
            rec["finished"] = time.time()
            logger.info(f"刷流直传：DONE {folder}/{target_name}")
            return "done", "已上传"
        task = self._copy_task_of(rec["src"], folder)
        if task:
            status = str(task.get("status") or "")
            if status == "Failed":
                return "error", f"复制任务失败: {task.get('errors') or '未知'}"
            if status == "Completed":
                try:
                    up = int(task.get("uploadedBytes") or 0)
                    total = int(task.get("totalBytes") or 0)
                    failed = int(task.get("failedFiles") or 0)
                except Exception:
                    up = total = failed = 0
                if failed or (total and up != total):
                    return "error", f"复制任务字节不一致（{up}/{total}，failed={failed}）"
                return "copying", "复制任务已完成，等待目标文件出现"
        return "copying", "复制中"
