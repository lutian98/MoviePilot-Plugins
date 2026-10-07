#!/usr/bin/env python3
"""Emby → MoviePilot Webhook 转换转发（零依赖，单文件可跑）。

为什么需要它
------------
MoviePilot 的 Emby 报文解析器（`app/modules/emby/emby.py::get_webhook_message`，
v3.0.9 与最新 v3 分支实测一致）只认两种输入：

    if form and form.get("data"):  result = form.get("data")        # ① 表单字段 data
    else:                          result = json.dumps(dict(args))   # ② 查询参数
    message = json.loads(result)
    if not message.get("Event"): return None                        # ← JSON body 到此被丢弃

而 Emby 的 Webhooks 插件发的是 **JSON body** → MP 解析不到 `Event` → 事件被静默丢弃，
MP 对所有第三方插件（含本项目的「Emby 联动删除」）都不会派发。这是 MP 侧的既有行为，
不是插件缺陷，也无法在插件里绕过 —— 只能在中间把格式转一下。

本脚本做的事
------------
接住 Emby 发来的报文（JSON / 表单都吃），转成 MP 认识的表单格式，再投给
    {MP_BASE}/api/v1/webhook/?token=<API_TOKEN>&source=<SOURCE>
只有白名单内的事件会被转发（默认仅删除类），其余原样放过。

用法
----
    # 方式一：直接跑
    MP_BASE=http://192.168.1.10:3000 MP_TOKEN=xxxx SOURCE=Emby \
    LISTEN_PORT=9432 python3 mp_emby_webhook_relay.py

    # 方式二：docker（见同目录 docker-compose.yml）
    docker compose up -d

然后把 **Emby → 插件 → Webhooks → Webhook URL** 指到本服务，例如
    http://192.168.1.10:9432/emby
（原有的 MP 地址可以保留也可以删掉：Emby 支持多个 webhook 目标，
  直接指向 MP 的那个不会生效，留着无害。）

自检
----
    curl -X POST -H 'Content-Type: application/json' \
         -d '{"Event":"library.deleted","Item":{"Name":"测试","Path":"/media/x.strm","Type":"Movie"}}' \
         http://127.0.0.1:9432/emby
返回 {"ok": true, "forwarded": "ok:200"} 即表示已成功投递给 MP。

环境变量
--------
    MP_BASE      MoviePilot 地址，默认 http://127.0.0.1:3000
    MP_TOKEN     MoviePilot 的 API Token（MP 设置 → API Token）
    SOURCE       媒体服务器名，必须与 MP 里的名称完全一致（区分大小写）。
                 本机 Emby 模块名是 `Emby`，写 `emby` 会被 MP 静默丢弃。
    LISTEN_PORT  监听端口，默认 9432
    LISTEN_HOST  监听地址，默认 0.0.0.0
    EVENTS       允许转发的事件名，逗号分隔；默认删除三类
    FORWARD_ALL  设为 1 则转发全部事件（含入库/播放），默认仅删除类
    URL_KEY      可选的共享密钥：设置后请求必须带 ?key=<URL_KEY>
    TIMEOUT      投递超时秒数，默认 30
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_EVENTS = ("library.deleted", "ItemDeleted", "item.deleted")


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


CFG = {
    "mp_base": _env("MP_BASE", "http://127.0.0.1:3000").rstrip("/"),
    "mp_token": _env("MP_TOKEN"),
    "source": _env("SOURCE", "Emby"),
    "listen_host": _env("LISTEN_HOST", "0.0.0.0"),
    "listen_port": int(_env("LISTEN_PORT", "9432") or 9432),
    "url_key": _env("URL_KEY"),
    "timeout": int(_env("TIMEOUT", "30") or 30),
    "forward_all": _env("FORWARD_ALL") in ("1", "true", "yes", "on"),
    "events": tuple(x.strip() for x in _env("EVENTS").split(",") if x.strip()) or DEFAULT_EVENTS,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mp-relay")


def _event_of(payload: dict) -> str:
    return str(payload.get("Event") or payload.get("event") or "")


def build_mp_payload(payload: dict) -> dict | None:
    """把媒体服务器报文整理成 MP 解析器要的形状。"""
    event = _event_of(payload)
    if not event:
        return None
    item = payload.get("Item") or payload.get("item") or {}
    if not isinstance(item, dict):
        item = {}
    out = {
        "Type": item.get("Type") or item.get("type"),
        "Name": item.get("Name") or item.get("name"),
        "Path": item.get("Path") or item.get("path"),
        "Id": item.get("Id") or item.get("id"),
        "ProductionYear": item.get("ProductionYear"),
        "ProviderIds": item.get("ProviderIds") or {},
    }
    out = {k: v for k, v in out.items() if v not in (None, "", {}, [])}
    return {"Event": event, "Item": out}


def forward(payload: dict) -> str:
    """转格式并投递给 MoviePilot。返回结果描述。"""
    event = _event_of(payload)
    if not CFG["forward_all"] and event not in CFG["events"]:
        return f"skip:{event or 'no-event'}"
    if not CFG["mp_token"]:
        return "error:no-token"
    mp_payload = build_mp_payload(payload)
    if not mp_payload:
        return "skip:empty"
    query = urllib.parse.urlencode({"token": CFG["mp_token"], "source": CFG["source"]})
    body = urllib.parse.urlencode({"data": json.dumps(mp_payload, ensure_ascii=False)}).encode()
    req = urllib.request.Request(
        f"{CFG['mp_base']}/api/v1/webhook/?{query}",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-API-KEY": CFG["mp_token"],
            "User-Agent": "mp-emby-webhook-relay/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CFG["timeout"]) as resp:
            code = getattr(resp, "status", 200)
        log.info("已投递 %s %s -> HTTP %s", event, mp_payload["Item"].get("Name"), code)
        return f"ok:{code}"
    except Exception as e:  # noqa: BLE001
        log.error("投递失败 %s: %s", event, e)
        return f"error:{type(e).__name__}"


def parse_body(raw: bytes, ctype: str) -> dict | None:
    """尽力从 Emby 的报文体里取出事件字典（JSON / 表单 / 内嵌 JSON 都试）。"""
    text = raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    if "multipart/form-data" in ctype.lower():
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None
    fields = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
    for key in ("data", "payload", "body"):
        if key in fields:
            try:
                obj = json.loads(fields[key])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return fields or None


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") not in ("", "/emby", "/webhook"):
                return self._json(404, {"ok": False, "err": "not found"})
            if CFG["url_key"]:
                qkey = (urllib.parse.parse_qs(parsed.query).get("key") or [""])[0]
                if qkey != CFG["url_key"]:
                    return self._json(403, {"ok": False, "err": "bad key"})
            if self.command not in ("POST", "PUT"):
                return self._json(405, {"ok": False, "err": "method"})
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
            payload = parse_body(raw, self.headers.get("Content-Type") or "")
            if not payload:
                log.warning("无法解析报文体（前 200 字节）：%s", raw[:200])
                return self._json(400, {"ok": False, "err": "bad body"})
            # 后台投递，绝不阻塞 Emby（Emby 超时会重试/报错）
            result: list[str] = []

            def _work() -> None:
                result.append(forward(payload))

            t = threading.Thread(target=_work, daemon=True)
            t.start()
            t.join(timeout=CFG["timeout"] + 5)
            return self._json(200, {"ok": True, "forwarded": result[0] if result else "pending"})
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"ok": False, "err": f"{type(e).__name__}: {e}"})

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, *args) -> None:  # 交给 logging，别往 stderr 刷
        return


def main() -> None:
    if not CFG["mp_token"]:
        log.warning("未设置 MP_TOKEN —— 无法投递给 MoviePilot，请设置后重启")
    httpd = ThreadingHTTPServer((CFG["listen_host"], CFG["listen_port"]), Handler)
    log.info(
        "监听 %s:%s → %s (source=%s, %s)",
        CFG["listen_host"], CFG["listen_port"], CFG["mp_base"], CFG["source"],
        "全部事件" if CFG["forward_all"] else f"仅 {','.join(CFG['events'])}",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("退出")


if __name__ == "__main__":
    main()
