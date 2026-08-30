# astrbot_plugin_groupmaster

**仅群聊可用的 AstrBot QQ 群管插件**（适配 aiocqhttp / NapCat / OneBot v11）。

命令直发 + 跨群批量 + 引用撤回 + LLM 自然语言兜底，一套命令覆盖日常群管全部操作。目标用户既可以 @，也可以直接输 QQ 号。

> 在 AstrBot **v4.27.4** + NapCat 环境下开发与实测。

## ✨ 特性

- 🎯 **双目标定位**：`@昵称` 或直接输 QQ 号均可
- 🌐 **跨群批量**：任意命令追加 `all`，作用于机器人担任管理员/群主的**所有群**，逐群回报结果
- 💬 **引用撤回**：引用一条消息发 `recall` 即撤回；也可按用户撤回最近 N 条
- 🤖 **LLM 兜底**：命令没记住也没关系，@Bot 说人话即可，插件注册 8 个 llm_tool 供大模型调用
- 🔒 **多层权限**：AstrBot 管理员 / 插件额外管理员 / 群管理员·群主，三层任一即可；私聊一律拒绝
- 🚫 **持久化拉黑**：`ban` 后自动拒绝该用户后续入群申请（重启不丢，存 state.json）
- ⏱ **警告系统**：`warn` 计数，达上限自动移出群聊，上限可随时改

## 📦 安装

**方式一：AstrBot WebUI（推荐）**

WebUI → 插件管理 → 从仓库安装，填入：

```
https://github.com/WolfeOvO/astrbot_plugin_groupmaster
```

**方式二：手动克隆**

```bash
cd AstrBot/data/plugins/
git clone https://github.com/WolfeOvO/astrbot_plugin_groupmaster
# 重启 AstrBot 或在 WebUI 重载插件
```

**前置要求**

- AstrBot ≥ 4.26（v4.27.4 实测通过）
- 已接入 aiocqhttp 适配器（NapCat / Lagrange.OneBot 等OneBot v11 实现）
- 执行禁言/踢人等操作时，机器人需具备该群**管理员**身份；`admin set/remove` 需机器人为**群主**

## 🧰 命令总览

> 需通过唤醒前缀（取决于你的 AstrBot 配置，如 `#`）或 @Bot 触发。以下 `<@用户>` 均可替换为裸 QQ 号。**所有命令仅群聊可用，私聊会被拒绝。**

| 命令 | 说明 |
| --- | --- |
| `timeout <秒数> <@用户>` | 禁言该用户 N 秒（缺省 600，上限 2592000 即 30 天） |
| `kick <@用户>` | 将该用户移出群聊 |
| `ban <@用户>` | 移出群聊**并拉黑**，自动拒绝其后续入群申请 |
| `unban <@用户>` | 解除拉黑，允许重新申请入群 |
| `warn <@用户>` | 警告次数 +1，达到上限自动移出群聊 |
| `warn max <次数>` | 设定警告次数上限（全局，即时生效并持久化） |
| （引用消息）`recall` | 撤回被引用的那条消息 |
| `recall <条数> <@用户>` | 撤回该用户最近 N 条消息（单次最多 50） |
| `mute` | 单发一次：切换开/关**全员禁言** |
| `admin set <@用户>` | 设为群管理员（**需机器人为群主**） |
| `admin remove <@用户>` | 取消群管理员（**需机器人为群主**） |

### 跨群批量（`all` 子命令）

任何操作命令后加 `all`，即对机器人担任管理员或群主的**所有群**逐群执行，并回报每群结果：

```
#timeout all 60 @张三      # 所有管理群内禁言张三 60 秒
#kick all @张三            # 所有管理群内踢出张三
#ban all 123456789         # 所有管理群内拉黑并踢出
#warn all @张三            # 所有管理群内警告 +1
#recall all 10 @张三       # 所有管理群内各撤回张三最近 10 条
#mute all                  # 所有管理群全员禁言
```

> 注意：引用消息 + `recall` 不参与全局批量，仅作用于当前群。批量结果按群逐行回报 ✅/❌。

### 使用示例

```
#timeout 600 @张三(12345678)      # 禁言 10 分钟
#timeout 3600 12345678            # 直接输 QQ 号，禁言 1 小时
#kick @张三(12345678)             # 踢出
#ban @张三(12345678)              # 踢出并拉黑
#warn @张三(12345678)             # 警告 1/3
#warn max 5                       # 警告上限改为 5
#recall 10 @张三(12345678)        # 撤回其最近 10 条
（引用某条消息）#recall            # 撤回那条消息
#mute                             # 开/关全员禁言
#admin set @张三(12345678)        # 设为管理员（bot 需群主）
```

## 🤖 LLM 自然语言兜底

与命令一样，仅群聊可用、同样受权限门控。未命中命令时由大模型解析意图并调用插件注册的工具：

| 工具 | 对应操作 |
| --- | --- |
| `gm_timeout_user` | 禁言 |
| `gm_kick_user` | 踢出 |
| `gm_ban_user` | 拉黑踢出 |
| `gm_unban_user` | 解除拉黑 |
| `gm_warn_user` | 警告 |
| `gm_recall_user_messages` | 按条数撤回 |
| `gm_toggle_whole_mute` | 切换全员禁言 |
| `gm_set_admin` | 设置/取消管理员 |

示例：`@Bot 把他禁言十分钟`、`@Bot 踢了刚才发广告的人`、`@Bot 撤回他最近 5 条消息`。

## 🔐 权限说明

**使用者**需满足以下任一条件：

1. AstrBot 管理员（WebUI 中配置的管理员 ID）
2. 插件配置 `extra_admins` 名单中的 QQ 号
3. 当前群的群管理员（admin）或群主（owner）

**机器人自身**权限要求：

- `timeout / kick / ban / warn（踢出）/ recall / mute`：机器人需为该群管理员
- `admin set / remove`：机器人需为该群群主
- 权限不足时会明确回报当前角色

## ⚙️ 配置项

安装后自动生成配置文件（WebUI 插件页可改）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `warn_max` | `3` | 默认警告次数上限（`warn max` 可运行时修改） |
| `extra_admins` | `[]` | 额外管理员 QQ 号列表 |
| `default_mute_seconds` | `600` | `timeout` 未给秒数时的默认时长 |
| `max_mute_seconds` | `2592000` | 单次禁言上限（QQ 平台最长 30 天） |
| `recall_scan_limit` | `100` | 撤回时扫描的历史消息条数上限 |
| `reject_reason` | `你已被本群拉黑，如有疑问请联系群管理` | 拒绝被拉黑用户入群时的理由 |

## 💾 数据存储

警告计数、拉黑名单与警告上限保存在：

```
AstrBot/data/plugin_data/astrbot_plugin_groupmaster/state.json
```

- 结构：`warn_max`（全局上限）、`warns.{群号}.{QQ号}`（计数）、`bans.{群号}.{QQ号}`（拉黑时间戳）
- `warn max` 的值优先于配置文件 `warn_max`
- 备份此文件即备份全部插件状态

## 🛠 工作原理

通过 `event.bot.call_action()` 直调 OneBot v11 动作：

| 动作 | 用途 |
| --- | --- |
| `set_group_ban` | 禁言 |
| `set_group_kick` | 踢出（`reject_add_request=true` 实现拉黑） |
| `delete_msg` | 撤回消息 |
| `set_group_whole_ban` | 全员禁言开关 |
| `set_group_admin` | 设置/取消管理员 |
| `set_group_add_request` | 自动拒绝被拉黑用户入群申请 |
| `get_group_member_info` | 查询用户/机器人角色（权限门控） |
| `get_group_list` | `all` 批量时枚举机器人管理的群 |
| `get_group_msg_history` | 按用户撤回时翻页收集目标消息 |
| `get_group_setting` | 查询全员禁言当前状态（NapCat 扩展，不可用时退回内存记录） |

监听 OneBot `request.group.add` 事件实现拉黑用户的入群申请自动拒绝。

## ⚠️ 已知边界

- **仅群聊**：私聊中所有命令与 LLM 工具一律拒绝
- **撤回上限**：单条命令最多撤回 50 条，扫描历史默认 100 条（可配）；超过时限（QQ 平台约 2 分钟内可任意撤回，普通管理员受群设置限制）的消息可能撤回失败，失败会逐条回报
- **全员禁言状态记忆**：优先用 NapCat `get_group_setting` 查真实状态；非 NapCat 端退回内存记录，重启后首次 `mute` 可能方向相反，再发一次即可
- **禁言时长**：QQ 平台单次禁言最长 30 天（2592000 秒），超出自动截断
- **命令冲突**：本插件命令均为英文小写，与常见中文命令群管插件（禁言/踢人等）不冲突
- 拉黑名单按群隔离：`ban` 只影响当前（或批量目标）群，`unban` 需在对应群执行

## 📄 许可证

[MIT](LICENSE) © WolfeOvO
