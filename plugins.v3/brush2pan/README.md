# 刷流直传 115（Brush2Pan）

刷流（BrushFlow）下载完成的影片 → 识别/筛选 → 经 CD2 直传 115 固定目录，并按 TMDB 重命名。

## 流程

1. MP 每 60 秒读取 QB 中 `save_path=/downloads/PT/刷流` 且已完成的种子
2. 文件名解析 → TMDB 识别
3. 口径筛选：
   - 只收电影（不收剧集）
   - 只收 1080p / 2160p
   - 不收蓝光原盘（BDMV / m2ts / ISO）
   - 排除 5.1 / 7.1 / Atmos / TrueHD / DTS-HD
   - 默认最小 300MB
4. CD2 gRPC `CreateFolder` → `CopyFile`（CD2 服务端直接读 `/Unraid` 本地文件并上传 `/115`，不落本地中转）
5. 复制完成后 `RenameFile` 为 `片名 (年份) - 1080p.mkv`
6. 状态保存在插件数据目录（`/config/plugins/Brush2Pan`）

## 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 演练模式 | 默认开：只记录/打印计划，不复制 |
| CD2 gRPC 地址 | 默认 `172.17.0.1:19798` |
| CD2 API Token | CD2 设置里创建，需读写 + 本地列目录权限 |
| 本地源路径 | CD2 视角的刷流目录，默认 `/Unraid/Media/PT/刷流` |
| 115 目标目录 | 默认 `/115/浅深资料/影片` |
| QB 保存路径过滤 | 默认 `/downloads/PT/刷流` |
| 最小体积 | 默认 300MB |
| TMDB API Key | 留空则用 MP 内置 |

## 实现说明

- 上传完全由 CD2 完成，插件不搬运文件、不经过 MP 的转存/整理
- `grpcurl` 二进制首次运行时下载到插件数据目录（走 MP 的 GITHUB_PROXY）
- 依赖：MP 自带 `requests`、`qbittorrent-api`，无需额外 pip 包

## 版本

- v0.1.0 首个版本
