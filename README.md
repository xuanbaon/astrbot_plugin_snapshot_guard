# AstrBot 快照守卫（snapshot_guard）

> 出问题之前先留一手，出问题之后一条命令退回去。

AstrBot 自带「备份 / 导入」，但它是一个**手动**的全量包：要自己记得点，而且导入时整库替换。
真正常见的翻车不是「整台机器没了」，而是这些：

- 调了个插件配置，第二天发现群里的回复全乱了，想不起改了什么
- 插件升级到新版本，行为变了，旧的配置项被覆盖
- 手滑删了 `data/plugin_data/xxx/` 里的文件
- 进程半夜被 OOM 杀掉，起来以后状态不对，但没有任何记录

快照守卫补的就是这一段：**定时存小快照、崩了能知道、能退回去、退错了还能再退回来**。

## 它做什么

| 能力 | 说明 |
| --- | --- |
| 定时轻量快照 | 默认每 6 小时一份，只收配置与插件状态，实测一份约 24MB |
| 启动即备份 | 每次启动先存一份，改坏之前的状态总在 |
| 崩溃自检 | 心跳写盘，启动时发现「上次没干净退出」就通知你 |
| 一条命令回档 | `/快照 回档 last`，回档前**自动再存一份**，退错了能退回来 |
| 自动清理 | 只留最近 N 份，不会把磁盘填满 |

## 命令

| 命令 | 作用 |
| --- | --- |
| `/快照` | 立刻存一份，返回文件名与体积 |
| `/快照 列表` | 已有快照，按时间倒序 |
| `/快照 回档 last` | 回档到最新一份；也可以写文件名片段 |
| `/快照 状态` | 调度、排除项、上次退出情况 |

命令仅管理员可用（`PermissionType.ADMIN`）。

## 快照里有什么，没有什么

**有**：`cmd_config.json`、人格配置、`plugin_data/` 下各插件的配置与状态、会话/记忆这类小文件。

**没有**（默认排除，可通过配置改）：

- `attachments`、`logs`、`backups`、`temp`、`webchat`、`knowledge_base`
- `data/plugins`（插件代码，约几十 MB；想要一起回档就把这一项删掉或打开 `include_plugin_code`）
- 素材型目录：`*/packs/*`、`*/images/*`、`*/media/*`、`*/downloads/*`、`*_images/*`
  —— 用通配符排除，**不假定你装了哪些插件**，表情包库有多大都不会被卷进来
- 单文件超过 `max_file_mb`（默认 20MB）的
- **所有 SQLite 主库与 `-wal` / `-shm`**

### 关于数据库，说清楚一件事

回档**不会覆盖任何 `.db` 文件**，哪怕它在压缩包里。

原因是踩过：AstrBot 运行中主库是打开状态，直接覆盖 `data_v4.db` 再配上不匹配的 `-wal`，
SQLite 会立刻报 `database disk image is malformed`，之后所有写库接口全部 500，只能重启核心。

所以这个插件的定位很明确：**管配置与状态的回档，不管数据库**。要整库备份/恢复，用 WebUI 的「备份」页。
快照包里仍然会收那些小的插件库文件（十几 KB 到几 MB），需要时你可以手动从 zip 里取出来。

## 配置

```yaml
schedule:
  auto_interval_hours: 6      # 0 = 关闭自动快照
  keep_snapshots: 8
  snapshot_on_start: true
  heartbeat_seconds: 60
scope:
  max_file_mb: 20
  excludes: [...]
  exclude_globs: ["*/packs/*", "*/images/*", "*/media/*", "*/downloads/*", "*_images/*"]
  include_plugin_code: false
notify:
  notify_on_crash: true
  admin_session: "snowluma:FriendMessage:123456789"
```

`admin_session` 里前半段是本机**平台 ID**，不是 `aiocqhttp` 这类类型名。
不知道写什么就填 `"平台ID:FriendMessage:你的QQ号"`，填错只会通知发不出去，不影响快照。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xuanbaon/astrbot_plugin_snapshot_guard.git
# 重启 AstrBot，或在 WebUI 插件页重载
```

也可以在 WebUI 插件市场搜「快照守卫」，或把 Release 里的 zip 拖进插件页。

无第三方依赖。装好先发 `/快照`，能返回「N 个文件 / X MB」就说明通路正常；
再发 `/快照 状态` 确认排除项数量合理（默认 9 条路径 + 5 条通配）。

## 兼容性

- AstrBot `>= 4.17.0`（依赖 `filter.on_astrbot_loaded`）
- 平台无关，纯本地文件操作
- 快照存在 `data/plugin_data/astrbot_plugin_snapshot_guard/snapshots/`

## License

MIT
