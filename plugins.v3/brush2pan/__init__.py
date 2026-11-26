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

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".wmv", ".flv", ".webm"}
BAD_GENRE_IDS = {27: "恐怖", 53: "惊悚"}
IMDB_DATASET_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
IMDB_MAX_AGE_DAYS = 7
AUDIO_BAD = re.compile(r"(?i)5\.1")
AUX_RE = re.compile(r"(?i)(sample|trailer|featurette|behind[ ._-]?the[ ._-]?scenes|extras?|bonus|teaser|花絮|预告|样片)")
EP_RE = re.compile(r"(?i)S\d{2,3}(E\d{1,3})?")
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
    plugin_version = "0.3.4"
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
            self._imdb_adult_check = bool(cfg.get("imdb_adult_check", True))
            self._imdb_lock = threading.Lock()
            if self._imdb_adult_check:
                try:
                    self._imdb_ensure(background=True)
                except Exception:
                    pass
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
            "path": "/audiocheck",
            "endpoint": self.api_audiocheck,
            "methods": ["GET"],
            "summary": "实际音轨检测",
            "description": "传入种子内视频相对路径，用 ffprobe 读实际音频流，5.1（6 声道）拒绝",
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
        st["enabled"] = bool(getattr(self, "_imdb_adult_check", True))
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
                "reason": "索引未就绪" if bad is None else ("色情（IMDb Adult）" if bad else "非成人")}

    def api_audiocheck(self, name: str = ""):
        if not name or ".." in name:
            return {"ok": False, "error": "name 必须是种子内相对路径"}
        ok, reason = self._audio_check({"name": name, "size": 0})
        return {"ok": ok, "reason": reason, "path": os.path.join(self._save_prefix, name.lstrip("/"))}

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
        esc = lambda x: (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
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
                     "error": "出错"}
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
            else:
                status = "downloading" if progress < 0.999 else "pending"
                reason = f"下载中 {progress*100:.1f}%" if progress < 0.999 else "等待下一轮扫描"
            rows.append({
                "status_cn": {"done": "已上传", "review": "拒绝/跳过", "downloading": "下载中",
                              "pending": "待处理", "copying": "复制中", "planned": "演练计划",
                              "error": "出错"}.get(status, status),
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
        返回 (reject: Optional[bool], reason)；索引未就绪时 return (None, ...) 不阻断。"""
        if not getattr(self, "_imdb_adult_check", True):
            return None, "IMDb 过滤未启用"
        imdb_id = None
        try:
            r = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb.get('id')}/external_ids",
                             params={"api_key": self._tmdb_key}, timeout=20, proxies=self._proxies())
            if r.status_code == 200:
                imdb_id = (r.json() or {}).get("imdb_id")
        except Exception:
            pass
        if not imdb_id:
            return None, "无 IMDb ID"
        st = self._imdb_status()
        if not st["index"]:
            self._imdb_ensure(background=True)
            return None, "IMDb 索引未就绪（后台构建中）"
        bad = imdb_id in (self._imdb_set() or set())
        return (True, f"色情（IMDb Adult {imdb_id}）") if bad else (False, f"IMDb {imdb_id} 非成人")

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

    def _audio_check(self, video: dict) -> Tuple[bool, str]:
        """读实际音频流：默认（第一条）音轨 6 声道 = 5.1 → 拒绝。与中字检测同样走 ffprobe。"""
        name = video.get("name", "")
        path = os.path.join(self._save_prefix, name.lstrip("/"))
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
        st = streams[0]
        try:
            channels = int(st.get("channels") or 0)
        except Exception:
            channels = 0
        codec = st.get("codec_name") or "?"
        tag = f"{codec} {channels}ch" if channels else codec
        if channels == 6:
            return False, f"实际音轨 5.1（{tag}，口径：排除）"
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
            return False, "5.1 声道（口径：排除）"
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
        """从文件列表里挑主视频：剔除样片/花絮/小文件，识别剧集/多碟合集。
        返回 (video_attr, code, count)；code: ok / no_files / tv / multi
        """
        vids = [f for f in files
                if os.path.splitext(getattr(f, "name", ""))[1].lower() in VIDEO_EXT
                and (getattr(f, "progress", 0) or 0) >= 0.999]
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
            if AUX_RE.search(getattr(f, "name", "") or "") or (main_size and size < main_size * 0.05):
                aux.append(f)
            else:
                rest.append(f)
        if AUX_RE.search(getattr(main, "name", "") or "") and not rest:
            return None, "aux_only", len(vids)
        if EP_RE.search(getattr(main, "name", "") or ""):
            return None, "tv", len(vids) - len(aux)
        if rest:
            return None, "multi", len(vids) - len(aux)
        return main, "ok", len(aux)

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
            if rec and rec.get("status") == "review" and str(rec.get("reason", "")).startswith("视频文件数="):
                state.pop(h, None)
                rec = None
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
            picked, code, ignored = self._select_video(files)
            if code == "no_files":
                # 文件列表可能尚未落盘/元数据未同步，下一轮重试，不写死 review
                logger.info(f"刷流直传：文件未就绪，稍后重试 {getattr(t,'name','')}")
                continue
            if code == "aux_only":
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": "仅样片/花絮，无正片", "ts": time.time()}
                logger.info(f"刷流直传：跳过 {getattr(t,'name','')}（仅样片/花絮）")
                self.save_data("state_v2", state)
                continue
            if code == "tv":
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": "剧集（口径：不收剧集）", "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → 剧集")
                self.save_data("state_v2", state)
                continue
            if code == "multi":
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": f"视频文件数={ignored}（疑似剧集/合集）", "ts": time.time()}
                logger.info(f"刷流直传：跳过 {getattr(t,'name','')}（实质视频数={ignored}）")
                self.save_data("state_v2", state)
                continue
            if ignored:
                logger.info(f"刷流直传：忽略 {ignored} 个样片/花絮文件 {getattr(t,'name','')}")
            video = {"name": picked.name, "size": picked.size}
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
            genre_ok, genre_reason, genre_detail = self._genre_check(tmdb, meta["is_tv"])
            if not genre_ok:
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": genre_reason, "meta": meta, "tmdb": tmdb.get("id"),
                            "genres": [g.get("name") for g in ((genre_detail or {}).get("genres") or []) if isinstance(g, dict)],
                            "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → {genre_reason}")
                self.save_data("state_v2", state)
                continue
            imdb_bad, imdb_reason = self._imdb_adult_check(tmdb)
            if imdb_bad:
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": imdb_reason, "meta": meta, "tmdb": tmdb.get("id"), "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → {imdb_reason}")
                self.save_data("state_v2", state)
                continue
            chs_meta = {"tmdb_lang": tmdb.get("original_language"),
                        "tmdb_title": tmdb.get("title") or tmdb.get("name")}
            chs_ok, chs_reason = (True, "未检查") if not self._require_chs else self._has_chs(video, files, chs_meta)
            audio_ok, audio_reason = self._audio_check(video)
            if not audio_ok:
                state[h] = {"status": "review", "name": getattr(t, "name", ""),
                            "reason": audio_reason, "meta": meta, "tmdb": tmdb.get("id"), "ts": time.time()}
                logger.info(f"刷流直传：REJECT {getattr(t,'name','')} → {audio_reason}")
                self.save_data("state_v2", state)
                continue
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
                   "size": video["size"], "audio": audio_reason, "ts": time.time()}
            if self._dry_run:
                logger.info(f"刷流直传：PLAN {src} → {dst_dir}/{fname}")
                state[h] = rec
                self.save_data("state_v2", state)
                continue
            try:
                if self._cd2_find(dst_dir, f"{dst_dir}/{fname}"):
                    rec["status"] = "done"
                    rec["finished"] = time.time()
                    state[h] = rec
                    logger.info(f"刷流直传：目标已存在，跳过 {dst_dir}/{fname}")
                    self.save_data("state_v2", state)
                    continue
            except Exception:
                pass
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
