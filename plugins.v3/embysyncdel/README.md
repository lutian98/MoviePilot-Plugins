# Emby 联动删除（EmbySyncDel）

Emby 删除影片 / 剧集后，同步清理 MoviePilot 侧的整理记录、源文件、媒体库文件与下载任务。

- 插件 ID：`EmbySyncDel`
- 插件目录：`plugins.v3/embysyncdel/`
- 适用 MoviePilot：`>=3.0.0`（V3）
- 默认模式：**演练**（只出清单，不删除）

## 为什么用它

同类插件在两类环境下会整体静默失效，本插件就是为解决它们而写：

| 问题 | 已有插件的做法 | 本插件 |
| --- | --- | --- |
| 事件名写死 | 只认 `library.deleted` / `ItemDeleted`，换个事件名就整体哑掉 | 事件名可配置，默认收 3 种 |
| strm / 软链接库匹配不上 | 要求上报路径与整理记录 `dest` **完全相等**；strm 报 `影片.strm`、记录里是 `影片.mkv` → 永久失配 → 静默放弃 | 按媒体身份 + **目录 / 文件名主干**匹配，与后缀无关 |
| 身份缺失即放弃 | 拿不到唯一媒体身份就退出 | 三级兜底：事件身份 → ProviderIds → 媒体路径匹配（要求路径对应、命中记录唯一，并叠加标题防误删校验） |

## 配置要点

- **媒体库路径映射（必填）**：`媒体服务器路径#MoviePilot路径`，一行一个，例如
  `/mnt/user/Media/Strm/Media#/downloads/link`。分隔符 `#` 优先，`: ` 亦兼容。
- **演练模式（默认开）**：先跑一遍核对通知里的清单，稳定后再关闭。
- **下载任务处理方式**：`删除任务与文件` 或 `暂停任务（保留文件）`。
- 其它：排除路径、删除源文件开关、辅种处理、标题防误删校验、清理插件历史。

## 行为与护栏

1. 事件名不在允许清单 / 命中排除路径 → 完全静默。
2. 媒体身份缺失 → 改用**媒体路径匹配**（要求路径对应、命中唯一，并叠加标题防误删校验），绝不按标题猜。
3. 媒体库文件必须位于映射范围内；源文件必须是媒体文件；strm 占位与真实媒体文件分别处理。
4. 标题与删除媒体不符 → 跳过该条并保留记录。
5. **整理记录最后删**：外部清理未完成时保留记录，便于下次事件补删。
6. 删除文件后自底向上清理空目录，遇非空目录或受保护根目录即停止。
7. 全部动作写入插件历史（插件详情页可查）并可发通知。

## 事件流

```
媒体服务器删除条目 → MoviePilot /api/v1/webhook → 事件总线
→ sync_del_by_webhook → 身份解析 → 路径映射 → 匹配整理记录
→ 删媒体库文件 / 源文件 / 空目录 → 处理下载任务（主任务 + 转种 + 辅种）
→ 删 strm 占位 → 最后删整理记录 → 通知
```

## 测试

测试位于仓库 `tests/v3/embysyncdel/`，桩掉宿主 `app.*`，无需 MoviePilot 即可运行：

```bash
python -m unittest discover -s tests/v3 -t .
```

其中 `test_e2e_sandbox.py` 会在真实临时目录上仿真「媒体服务器媒体库 + MoviePilot 媒体库 +
保种目录」，跑通完整删除链路并验证隔离性（同目录其它影片必须毫发无损）。

## 许可证

GPL-3.0

## 在 Emby 里怎么配（唯一一条路，1.5.0 起）

插件通过 **MoviePilot 自带的事件总线**接收删除事件，所以在 Emby 侧只需要一条普通 Webhook：

1. Emby 控制台 → 设置 → 通知 / Webhooks → 新建
2. 地址填（**末尾的斜杠不能少**，`source=Emby` 大小写敏感）：

```
http://<你的MP地址>/api/v1/webhook/?token=<MoviePilot 的 API 密钥>&source=Emby
```

3. 事件勾选"媒体库删除 / 项目删除"那几项
4. **提交格式必须选 form-data**（表单字段名 `data`）

### 为什么必须 form-data？

MoviePilot 的 Emby 解析器（`app/modules/emby/emby.py::get_webhook_message`）只读**表单字段 `data`**
或**查询参数**，**不读 JSON 请求体**：

```python
if form and form.get("data"):
    result = form.get("data")
else:
    result = json.dumps(dict(args))
message = json.loads(result)
eventType = message.get("Event")
if not eventType:
    return None          # ← Emby 发 JSON body 时，事件到这里已被丢弃（HTTP 200、无日志）
```

Emby 的 Webhooks 插件同时支持 form-data 和 JSON 两种提交方式：**选 JSON 则事件会被 MoviePilot
静默丢弃**，任何挂在宿主事件总线上的插件都收不到（这也是同类"联动删除"插件集体失效的根因）；
**选 form-data 则一切正常**，插件无需任何专属入口。

> 1.4.x 及更早版本自带一个「直投入口」（`/api/v1/plugin/EmbySyncDel/emby_webhook?key=...`）用于绕过
> 上述限制。**1.5.0 已移除该入口**（它是个匿名入口，多一个可被伪造触发删除的面）。
> 从 1.4.4 起用直投入口的用户请把 Emby 的 Webhook 地址改回上面的默认入口并选 form-data。

### 报文格式不挑

插件解析时对字段写法很宽容，你换 Emby 模板一般不影响：

```
事件名：Event / event / eventType / type
条目：  Item（单个）/ Items（数组，逐条处理）
名称：  Name / name（年份自动补成「名称 (年份)」）
路径：  Path / path       身份：ProviderIds（没有则退回路径匹配）
```
