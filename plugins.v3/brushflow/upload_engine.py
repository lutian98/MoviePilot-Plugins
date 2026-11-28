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
POLICY_VER = 3
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


class UploadEngine:
    """刷流直传上传引擎：由 BrushFlow 宿主插件持有，不单独注册为插件。"""

    def __init__(self, host, cfg: Optional[Dict[str, Any]] = None) -> None:
        self._host = host
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._dry_run = bool(cfg.get("dry_run", True))
        self._cd2_grpc = str(cfg.get("cd2_grpc") or "")
        self._cd2_token = str(cfg.get("cd2_token") or "")
        self._dest_root = str(cfg.get("dest_root") or "/115/浅深资料/影片").rstrip("/")
        self._src_prefix = str(cfg.get("src_prefix") or "/Unraid/Media/PT/刷流").rstrip("/")
        self._save_prefix = str(cfg.get("save_path") or "/downloads/PT/刷流").rstrip("/")
        try:
            self._min_size_mb = int(cfg.get("min_size_mb") or 300)
        except Exception:
            self._min_size_mb = 300
        self._imdb_enabled = bool(cfg.get("imdb_adult_check", True))
        self._imdb_lock = threading.Lock()
        self._probe_cache = {}
        self._tmdb_cache = {}
        self._retry_attempts = 0
        self._downloader_helper = DownloaderHelper()
        self._tmdb_key = str(cfg.get("tmdb_key") or getattr(settings, "TMDB_API_KEY", "") or "")
        self._require_chs = bool(cfg.get("require_chs", True))
        try:
            self._since = float(cfg.get("since") or 0)
        except Exception:
            self._since = 0.0
        if self._imdb_enabled:
            try:
                threading.Thread(target=lambda: self._imdb_ensure(background=True), daemon=True).start()
            except Exception:
                pass

    def get_data(self, key: str):
        return self._host.get_data(f"upload_{key}")

    def save_data(self, key: str, value) -> None:
        self._host.save_data(f"upload_{key}", value)

    def get_data_path(self):
        return self._host.get_data_path()

    def get_state(self) -> bool:
        return bool(self._enabled)

    @property
    def plugin_name(self) -> str:
        return "刷流直传"

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
        # 额外搜索候选：保留标点（+ / : 等）的原始标题，避免 "3.0+1.0" 被拆成 "3 0 1 0"
        variants = [title]
        if cn:
            variants.append(cn)
        # 保留版本号里的点（3.0+1.0），其余点/下划线转空格
        raw = re.sub(r"[._]+", " ", re.sub(r"(?<!\d)\.|\.(?!\d)", " ", body).replace("_", " "))
        raw = re.sub(r"\s+", " ", raw).strip(" -_")
        m = re.search(r"(?i)\b(?:2160p|1080p|4k|bluray|blu-ray|web-?dl|webrip|hdrip|hdtv|remux|dvdrip|bdrip)\b", raw)
        if m:
            raw = raw[:m.start()].strip(" -_")
        if year:
            raw = re.sub(rf"(?<!\d){year}(?!\d)", " ", raw)
        raw = re.sub(r"(?i)\b(?:rerip|repack|proper|limited|internal|readnfo|diy)\b", " ", raw)
        raw = re.sub(r"\b(?:TWN|HKG|USA|GER|FRA|JPN|KOR|ITA|ESP|CAN|AUS|UK)\b", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip(" -_")
        if raw:
            variants.append(raw)
        no_movie = re.sub(r"(?i)\bthe\s+movie\b", " ", raw or title)
        no_movie = re.sub(r"\s+", " ", no_movie).strip(" -_")
        if no_movie:
            variants.append(no_movie)
        seen = set()
        variants = [x for x in variants if x and not (x in seen or seen.add(x))]
        return {"title": cn or title, "year": year, "resolution": res, "is_tv": tv, "cn": cn,
                "variants": variants}

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

    def _tmdb_search(self, title: str, year: Optional[int], is_tv: bool,
                     extra_variants: Optional[List[str]] = None) -> Optional[dict]:
        if not self._tmdb_key:
            logger.warning("刷流直传：没有 TMDB key")
            return None
        key = (title, year, bool(is_tv))
        cache = getattr(self, "_tmdb_cache", None)
        if cache is None:
            cache = self._tmdb_cache = {}
        if key in cache:
            return cache[key]
        candidates: List[str] = []
        # 先试主标题（中文名优先，避免英文副标题先命中同名不同片），再试原始标题变体
        for v in self._title_variants(title) + [title] + list(extra_variants or []):
            if v and v not in candidates:
                candidates.append(v)
        # 去掉尾部多余 token（地区/版本/组名），提升裸标题命中率
        for v in list(candidates):
            toks = v.split()
            for cut in (1, 2):
                if len(toks) - cut >= 2:
                    cand = " ".join(toks[:-cut])
                    if cand not in candidates:
                        candidates.append(cand)
        for variant in candidates:
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

    def _imdb_dir(self):
        """优先复用旧 Brush2Pan 插件已下载的 IMDb 数据集，避免重复 221MB 下载。"""
        legacy = Path("/config/plugins/Brush2Pan/imdb")
        try:
            if legacy.exists():
                return legacy
        except Exception:
            pass
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
            best = None      # (score, item)
            fallback = None  # 同年份、搜索排名靠前的结果（标题译名/原名无法直接比对时兜底）
            for item in results:
                name = item.get("title") or item.get("name") or ""
                orig = item.get("original_title") or item.get("original_name") or ""
                date = item.get("release_date") or item.get("first_air_date") or ""
                ry = int(date[:4]) if date[:4].isdigit() else None
                score = 0
                if want and (self._norm_title(name) == want or (orig and self._norm_title(orig) == want)):
                    score = 10
                else:
                    # 模糊：包含关系（副标题/长标题）或英文词元重叠
                    for cand in (name, orig):
                        cn = self._norm_title(cand)
                        if not cn or not want:
                            continue
                        if len(want) >= 6 and (want in cn or cn in want):
                            ratio = min(len(want), len(cn)) / max(len(want), len(cn))
                            if ratio >= 0.5:
                                score = max(score, 6)
                                break
                    if score == 0:
                        qt = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
                        nt = set(re.findall(r"[a-z0-9]+", (name + " " + orig).lower()))
                        if qt and nt:
                            j = len(qt & nt) / len(qt | nt)
                            if j >= 0.7:
                                score = 5
                if year and ry == year:
                    score += 5
                elif year and ry and abs(ry - year) <= 1:
                    score += 2
                if score >= 6 and (best is None or score > best[0]):
                    best = (score, item)
                if fallback is None and year and ry == year and len((title or "").strip()) >= 6:
                    fallback = item
            if best:
                return best[1]
            return fallback
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

    def _sub_text_sample(self, path: str, stream_index: int) -> str:
        """从指定字幕流抽文本（srt），用于判断是否真的含中文。"""
        ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
        try:
            r = subprocess.run([ffmpeg, "-v", "quiet", "-nostdin", "-i", path,
                                "-map", f"0:{stream_index}", "-t", "60", "-f", "srt", "-"],
                               capture_output=True, timeout=30)
        except Exception:
            return ""
        raw = r.stdout or b""
        for enc in ("utf-8", "utf-16", "gbk"):
            try:
                txt = raw.decode(enc, "ignore")
            except Exception:
                continue
            if txt.strip():
                return txt[:200000]
        return ""

    def _sub_streams(self, path: str) -> List[dict]:
        ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams",
                 "-select_streams", "s", path],
                capture_output=True, text=True, timeout=60)
            return json.loads(r.stdout or "{}").get("streams", []) or []
        except Exception:
            return []

    def _has_chs(self, video: dict, files: List[Any], meta: Optional[dict] = None) -> Tuple[bool, str]:
        """判断是否含中字：外挂字幕（解码后验中文）→ 文件名标注 → 内嵌流逐条验内容。"""
        name = video.get("name", "")
        path = self._video_path(video)
        for f in files:
            fname = getattr(f, "name", "")
            if os.path.splitext(fname)[1].lower() in self.SUB_EXT:
                try:
                    blob = open(os.path.join(self._save_prefix, fname.lstrip("/")), "rb").read(262144)
                except Exception:
                    continue
                for enc in ("utf-8", "utf-16", "gbk"):
                    try:
                        if self._cjk(blob.decode(enc, "ignore")):
                            return True, f"外挂中文字幕 {os.path.basename(fname)}"
                    except Exception:
                        continue
        if re.search(r"(中字|中文字幕|简繁|简中|繁中|简体|繁体|CHS|CHT|Chinese)", name, re.I):
            return True, "文件名标注中字"
        streams = self._sub_streams(path)
        if not streams:
            return False, "未发现中文字幕（无外挂、无内嵌字幕流）"
        text_codecs = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt", "text"}
        image_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
        forced_cjk = False
        def _meta(st):
            tags = st.get("tags") or {}
            lang = str(tags.get("language") or "").lower()
            title = str(tags.get("title") or "")
            disp = st.get("disposition") or {}
            forced = bool(disp.get("forced") or 0)
            codec = st.get("codec_name") or ""
            tagged = (lang in ("chi", "zho", "zh", "cmn", "yue", "chs", "cht") or
                      bool(re.search(r"(中|简|繁|CHS|CHT|Chinese)", title, re.I)))
            return lang, title, forced, codec, tagged
        # 第一优先：语言/标题标了中文的文本字幕；其次才抽 1~2 条无语言标记文本字幕
        text_streams = [st for st in streams if (st.get("codec_name") or "") in text_codecs]
        tagged_text = [st for st in text_streams if _meta(st)[4]]
        untagged_text = [st for st in text_streams if not _meta(st)[4]]
        for st in tagged_text:
            lang, title, forced, codec, _ = _meta(st)
            idx = st.get("index")
            sample = self._sub_text_sample(path, idx) if isinstance(idx, int) else ""
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]{2,}", sample or ""))
            if has_cjk:
                if forced:
                    forced_cjk = True
                    continue
                return True, f"内嵌文本中字 {lang or title or codec}"
        for st in untagged_text[:2]:
            lang, title, forced, codec, _ = _meta(st)
            idx = st.get("index")
            sample = self._sub_text_sample(path, idx) if isinstance(idx, int) else ""
            if re.search(r"[\u4e00-\u9fff]{2,}", sample or ""):
                if forced:
                    forced_cjk = True
                    continue
                return True, f"内嵌文本字幕含中文（{codec}）"
        for st in streams:
            lang, title, forced, codec, tagged = _meta(st)
            if codec in image_codecs and tagged:
                if forced:
                    forced_cjk = True
                    continue
                return True, f"内嵌图形中字 {lang or title or codec}"
        if forced_cjk:
            return False, "仅有强制/特效字幕（非完整中字）"
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
        tmdb = self._tmdb_search(meta["cn"] or meta["title"], meta["year"], meta["is_tv"],
                                extra_variants=meta.get("variants"))
        if not tmdb:
            if getattr(self, "_tmdb_transient", False):
                self._schedule_retry(state, h, name, "TMDB 查询失败（网络/接口）")
            else:
                # 不直接永久拒绝：自动重试 3 次（TMDB 索引/译名延迟、搜索偶发失败都可能自愈）
                self._schedule_retry(state, h, name, "TMDB 未识别（自动重试）")
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

    # ---------- 预筛 / 删除保护（供 BrushFlow 宿主调用） ----------
    def prefilter_torrent(self, title: str, description: str, size: int) -> Tuple[bool, str]:
        """下载前粗筛：只挡确定性不合适的，中字/真实音轨留给下载后精判。"""
        meta = self._parse_release(title or "")
        if meta.get("is_tv"):
            return False, "剧集"
        text = f"{title or ''} {description or ''}"
        if re.search(r"(?i)(COMPLETE\.?BLURAY|REMUX|\.ISO|BDMV|原盘|m2ts)", text):
            return False, "蓝光原盘"
        if AUDIO_BAD.search(text):
            return False, "TrueHD/DTS 音轨"
        if not re.search(r"(?i)(1080p|2160p)", title or ""):
            return False, "非 1080p/2160p"
        try:
            if int(size or 0) / 1024 / 1024 < self._min_size_mb:
                return False, f"文件过小 {int(size or 0)/1024/1024:.0f}MB"
        except Exception:
            pass
        tmdb = self._tmdb_search(meta["cn"] or meta["title"], meta["year"], meta["is_tv"],
                                extra_variants=meta.get("variants"))
        if tmdb:
            ok, reason, _ = self._genre_check(tmdb, meta["is_tv"])
            if not ok:
                return False, reason
        # 中字不确定：宽松放行，下载后 _has_chs 精判
        return True, ""

    def protected_hashes(self) -> set:
        """返回正在精判/上传/重试中的种子 hash，宿主删种时跳过。"""
        state = self.get_data("state_v2") or {}
        now = time.time()
        out = set()
        for h, rec in state.items():
            if not isinstance(rec, dict):
                continue
            st = rec.get("status")
            if st == "copying":
                out.add(h)
            elif st == "retry" and float(rec.get("next_ts") or 0) > now:
                out.add(h)
            elif rec.get("protect_until") and float(rec.get("protect_until") or 0) > now:
                out.add(h)
        return out
