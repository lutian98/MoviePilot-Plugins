# Emby → MoviePilot Webhook 转换转发

> **Emby 用户必装。** 不是本插件的依赖，而是 MoviePilot 与 Emby 之间的「翻译层」。

## 为什么需要它

MoviePilot 的 Emby 报文解析器（`app/modules/emby/emby.py::get_webhook_message`）
**只认表单字段 `data` 或查询参数，不读 JSON 请求体**（v3.0.9 与最新 v3 分支源码一致）：

```python
if form and form.get("data"):  result = form.get("data")        # ① 表单字段 data
else:                          result = json.dumps(dict(args))   # ② 查询参数
message = json.loads(result)
if not message.get("Event"): return None                        # ← JSON body 到此被丢弃
```

而 **Emby 的 Webhooks 插件发的是 JSON body**。结果：MP 拿不到 `Event`，
事件被**静默丢弃**（HTTP 仍回 200、日志无任何报错），任何第三方插件都收不到
Emby 的真实删除事件 —— 「Emby 删片 → MP 自动清理」这类功能因此对所有人都不可能生效。

这既不是本插件的缺陷，也无法在插件里绕过（插件拿到的是已经解析失败的空事件），
只能在中间把格式转一下。

> 参照：Plex 的 Webhook 本身就是表单提交（字段 `payload`），Jellyfin 由 MP 反向调
> API 取详情，都不受影响。**只有 Emby 需要这个转发。**

## 它做什么

接住 Emby 的报文（JSON / 表单都吃）→ 转成 MP 认识的形式 → 投给
`{MP_BASE}/api/v1/webhook/?token=<API_TOKEN>&source=<SOURCE>`。
默认只转发删除类事件（`library.deleted` / `ItemDeleted` / `item.deleted`），其余放过。

## 快速开始

### 方式一：直接跑（零依赖，Python 3.10+）

```bash
MP_BASE=http://192.168.1.10:3000 \
MP_TOKEN=你的MP_API_TOKEN \
SOURCE=Emby \
LISTEN_PORT=9432 \
python3 mp_emby_webhook_relay.py
```

### 方式二：docker compose

```bash
# 改好 docker-compose.yml 里的 MP_BASE / MP_TOKEN / SOURCE 后：
docker compose up -d
```

### 然后把 Emby 指过来

Emby → 控制台 → 插件 → **Webhooks** → 新增/修改 Webhook URL：

```
http://<本服务地址>:9432/emby
```

Emby 支持配置多个 webhook 目标，指向 MP 的那个（`http://mp:3000/api/v1/webhook?...`）
留着无害，但它自身不会生效。

### 自检

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"Event":"library.deleted","Item":{"Name":"测试","Path":"/media/x.strm","Type":"Movie"}}' \
  http://127.0.0.1:9432/emby
# → {"ok": true, "forwarded": "ok:200"}  表示已成功投递给 MP
```

投递是否真的生效，看 **MP 的通知记录**（`GET /api/v1/message/notification`）
或插件页面的执行历史，比翻日志可靠。

## 配置项

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `MP_BASE` | MoviePilot 地址 | `http://127.0.0.1:3000` |
| `MP_TOKEN` | MP → 设置 → API Token | 必填 |
| `SOURCE` | 媒体服务器名，**必须与 MP 里的名称完全一致且大小写一致**（本机为 `Emby`；写 `emby` 会被 MP 静默丢弃） | `Emby` |
| `LISTEN_PORT` | 监听端口 | `9432` |
| `LISTEN_HOST` | 监听地址 | `0.0.0.0` |
| `EVENTS` | 允许转发的事件名（逗号分隔） | 删除三类 |
| `FORWARD_ALL` | `1` = 转发全部事件 | 仅删除类 |
| `URL_KEY` | 可选共享密钥，请求需带 `?key=xxx` | 空 |
| `TIMEOUT` | 投递超时（秒） | `30` |

## 常见问题

**Emby 那边返回 200 但没反应？**
先看本服务日志里有没有 `已投递 … -> HTTP 200`。有 → 问题在 MP 侧（插件未启用、
路径映射不对、事件名不在监听列表）；没有 → 报文体没解析出来（把日志里那行
`无法解析报文体` 贴出来）。

**必须用 9432 吗？**
不必，任何端口都行，`LISTEN_PORT` 改掉即可。

**MP 里媒体服务器名怎么确认？**
`GET /api/v1/system/module-catalog` 里 `type=mediaserver` 且 `active=true` 的那一项的
`name` 字段，就是 `SOURCE` 该填的值。
