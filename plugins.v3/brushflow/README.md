# 刷流直传 115（一体版） 6.2.x

在 jxxghp/MoviePilot-Plugins 的「站点刷流」v6.1.7 基础上 fork（GPLv3，保留原作者署名），把 Brush2Pan 的下载后精判与 CD2 直传 115 合并进同一插件。

## 功能

- 完整保留 BrushFlow v6.1.7 全部功能：多站点任务、调度、免费/HR/体积/ratio、QB/TR、标签纳管、下载失败缓存、动态/全局删种、统计、通知、Vue 工作台与全部 API。
- 新增下载前预筛：剧集/原盘/TrueHD-DTS/分辨率/体积硬拒；TMDB 题材/成人；中字线索宽松（不确定放行），下载后精判。
- 新增随机兜底：一轮无预筛通过且下载器空闲时，从「通过基础条件但被预筛拒绝」池随机补 1 个。
- 新增下载后精判：正片/原盘/多视频、实际默认音轨、实际中字（抽内容验 CJK）、TMDB 时长；通过后 CD2 直传 115 并按 TMDB 重命名。
- 新增删除保护：精判/上传/重试中的种子暂缓 BrushFlow 删种，避免上传中被删。
- 新增 API：`/upload/settings`、`/upload/status`、`/upload/selftest`、`/upload/records`。
- 保留旧 BrushFlow 配置、任务、数据键；上传配置/预筛/随机兜底为新增配置项。

## 注意

- 本插件使用 `BrushFlow` 插件 ID（覆盖官方同 ID 插件），需关闭官方自动更新，从 `lutian98/MoviePilot-Plugins` 安装。
- `prefilter`、`random_fallback` 默认关闭，`upload.dry_run` 默认开启；灰度时通过 `/upload/settings` 开启。
- 旧 Brush2Pan 插件停用保留，用于回滚。
