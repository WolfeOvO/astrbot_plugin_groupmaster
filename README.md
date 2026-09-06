# astrbot_plugin_groupmaster

**功能齐全的 AstrBot QQ 群管插件**（适配 aiocqhttp / NapCat / OneBot v11）。

命令直发 + 理由存档 + 状态查询 + 跨群批量 + 超长禁言续期 + 敏感词/刷屏/广告检测 + 欢迎/退群消息 + LLM 自然语言兜底，一套命令覆盖日常群管全部操作。

> 在 AstrBot **v4.27.4** + NapCat 环境下开发与实测。

## ✨ 特性

### 核心功能
- 🎯 **双目标定位**：`@昵称` 或直接输 QQ 号均可
- 📝 **理由存档**：`timeout` / `kick` / `ban` / `warn` 可附理由，随操作持久化，`status` 里随时回查
- 📊 **状态查询**：`status` 一键查看本群禁言剩余时间、警告计数、拉黑名单与理由；带目标查单人档案
- 🌐 **跨群批量**：任意操作命令追加 `all`，作用于机器人担任管理员/群主的**所有群**，逐群回报结果
- 💬 **引用撤回**：引用一条消息发 `recall` 即撤回；也可按用户撤回最近 N 条
- 🤖 **LLM 兜底**：命令没记住也没关系，@Bot 说人话即可，插件注册 21 个 llm_tool，功能与命令侧全量对齐
- 🔒 **权限收口**：仅**本群群主/管理员**可使用（`get_group_member_info` 实时校验群角色，查询失败即拒绝）；私聊一律拒绝

### v1.1.0 新增功能
- ⏰ **超长禁言**：支持最长 365 天禁言，自动分段续期（OneBot 协议单次最长 30 天，插件后台自动续期）
- 📋 **禁言/拉黑列表**：`mutelist` 查看当前所有被禁言成员及剩余时间，`banlist` 查看拉黑名单
- 🎖️ **专属头衔**：`title` 设置群成员专属头衔（需机器人为群主）
- 🛡️ **敏感词检测**：`sw` 配置敏感词列表，自动撤回并禁言/警告违规消息
- 🚫 **刷屏检测**：`antiflood` 配置时间窗口内消息数阈值，自动禁言刷屏用户
- 📢 **广告检测**：`ad` 配置广告关键词，`adban` 开启后自动拉黑广告号
- 👋 **欢迎/退群**：`wel` 配置入群欢迎消息，`bye` 配置退群提示
- 📣 **群公告/改名**：`bc` 发送群公告，`g` 修改群名片/群名称
- 👤 **名片检测**：`cardcheck` 开启后自动检测新成员名片合规性

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
- 执行禁言/踢人等操作时，机器人需具备该群**管理员**身份；`admin set/remove` / `title` 需机器人为**群主**

## 🧰 命令总览

> 需通过唤醒前缀（取决于你的 AstrBot 配置，如 `#`）或 @Bot 触发。以下 `<@用户>` 均可替换为裸 QQ 号。**所有命令仅群聊可用、仅本群群主/管理员可使用，私聊会被拒绝。**

### 基础管理

| 命令 | 说明 |
| --- | --- |
| `timeout <秒数> <@用户> [理由]` | 禁言该用户 N 秒（缺省 600，上限 31536000 即 365 天，自动分段续期） |
| `timeout 0 <@用户>` | **解除**该用户的禁言 |
| `unmute <@用户>` | 解除禁言（同 `timeout 0`） |
| `mutelist` | 查看当前所有被禁言成员及剩余时间 |
| `muteall <秒数>` | 禁言所有普通成员（非管理员）指定秒数，0 为解除 |
| `kick <@用户> [理由]` | 将该用户移出群聊（不拉黑），可附理由存档 |
| `ban <@用户> [理由]` | 移出群聊**并拉黑**，自动拒绝其后续入群申请；理由存档 |
| `unban <@用户>` | 解除拉黑，允许重新申请入群 |
| `banlist` | 查看当前拉黑名单与理由 |
| `warn <@用户> [理由]` | 警告次数 +1，达到上限自动移出群聊；理由存档（保留最近 3 条） |
| `warn max <次数>` | 设定警告次数上限（全局，即时生效并持久化） |
| `warn clear <@用户>` | 清除该用户的警告计数（撤销误警告） |
| （引用消息）`recall` | 撤回被引用的那条消息 |
| `recall <条数> <@用户>` | 撤回该用户最近 N 条消息（单次最多 50，允许目标是机器人） |
| `mute` | 单发一次：切换开/关**全员禁言** |
| `admin set <@用户>` | 设为群管理员（**需机器人为群主**） |
| `admin remove <@用户>` | 取消群管理员（**需机器人为群主**） |
| `title <@用户> <头衔>` | 设置群成员专属头衔（**需机器人为群主**） |
| `status` | 本群总览：禁言剩余/警告计数/拉黑名单与理由 |
| `status <@用户>` | 单人档案：群身份、警告计数与理由、拉黑、禁言剩余 |

### 自动化检测

| 命令 | 说明 |
| --- | --- |
| `sw add <关键词>` | 添加敏感词 |
| `sw del <关键词>` | 删除敏感词 |
| `sw list` | 查看敏感词列表 |
| `sw on/off` | 开启/关闭敏感词检测 |
| `sw action mute/warn/kick` | 设置违规动作（禁言/警告/踢出） |
| `antiflood <条数> <秒数>` | 设置刷屏阈值（N 秒内 M 条消息），0 0 关闭 |
| `antiflood action mute/warn` | 设置刷屏处理动作 |
| `ad add <关键词>` | 添加广告关键词 |
| `ad del <关键词>` | 删除广告关键词 |
| `ad list` | 查看广告关键词列表 |
| `ad on/off` | 开启/关闭广告检测 |
| `adban on/off` | 开启/关闭广告自动拉黑（检测到即拉黑踢出） |
| `cardcheck on/off` | 开启/关闭新成员名片检测 |

### 群组功能

| 命令 | 说明 |
| --- | --- |
| `wel on/off` | 开启/关闭入群欢迎消息 |
| `wel set <消息>` | 设置欢迎消息（`{user}` 会替换为 @新成员） |
| `bye on/off` | 开启/关闭退群提示 |
| `bye set <消息>` | 设置退群消息（`{user}` 会替换为 QQ 号） |
| `bc <公告内容>` | 发送群公告 |
| `g name <新群名>` | 修改群名称 |
| `g card <@用户> <新名片>` | 修改群成员名片 |

### 跨群批量（`all` 子命令）

任何操作命令后加 `all`，即对机器人担任管理员或群主的**所有群**逐群执行，并回报每群结果：

```
#timeout all 60 @张三      # 所有管理群内禁言张三 60 秒
#kick all @张三            # 所有管理群内踢出张三
#ban all 123456789 广告号   # 所有管理群内拉黑并踢出，附理由
#warn all @张三 刷屏        # 所有管理群内警告 +1，附理由
#recall all 10 @张三       # 所有管理群内各撤回张三最近 10 条
#mute all                  # 所有管理群全员禁言
#sw all add 违禁词         # 所有管理群添加敏感词
```

> 注意：引用消息 + `recall` 不参与全局批量，仅作用于当前群；`status`/`mutelist`/`banlist` 仅查询单个群，不支持 `all`。批量结果按群逐行回报 ✅/❌。

## 📖 使用示例

### 基础管理
```
#timeout 600 @张三(12345678)      # 禁言 10 分钟
#timeout 86400 @张三 违规发言     # 禁言 1 天并记录理由
#timeout 2592000 @张三            # 禁言 30 天（上限，自动续期）
#timeout 0 @张三                  # 解除禁言
#unmute @张三                     # 解除禁言（同上）
#mutelist                         # 查看当前所有被禁言成员
#muteall 300                      # 禁言所有普通成员 5 分钟
#kick @张三                       # 踢出
#ban @张三 广告刷屏                # 踢出并拉黑，理由存档
#unban @张三                      # 解除拉黑
#banlist                          # 查看拉黑名单
#warn @张三 人身攻击                # 警告 1/3，理由存档
#warn max 5                       # 警告上限改为 5
#warn clear @张三                 # 清除其警告计数
#status                           # 本群总览
#status @张三                     # 张三的单人档案
#recall 10 @张三                  # 撤回其最近 10 条
（引用某条消息）#recall            # 撤回那条消息
#mute                             # 开/关全员禁言
#admin set @张三                  # 设为管理员（bot 需群主）
#title @张三 群管理                # 设置专属头衔（bot 需群主）
```

### 自动化检测
```
#sw add 涉政关键词                 # 添加敏感词
#sw list                          # 查看敏感词列表
#sw on                            # 开启敏感词检测
#sw action mute                   # 违规后禁言 10 分钟
#antiflood 5 10                   # 10 秒内发 5 条消息视为刷屏
#antiflood action warn            # 刷屏后警告 +1
#ad add 加V                       # 添加广告关键词
#ad on                            # 开启广告检测
#adban on                         # 开启自动拉黑（检测到即踢出拉黑）
#cardcheck on                     # 开启新成员名片检测
```

### 群组功能
```
#wel on                           # 开启入群欢迎
#wel set 欢迎 {user} 加入本群！    # 设置欢迎消息
#bye on                           # 开启退群提示
#bye set {user} 离开了我们          # 设置退群消息
#bc 本周五晚8点开会                # 发送群公告
#g name 技术交流群                 # 修改群名
#g card @张三 张三-运维            # 修改张三的群名片
```

## 🤖 LLM 工具

插件注册了 21 个 llm_tool，@Bot 说人话即可触发：

```
@Bot 把张三禁言 10 分钟           → gm_timeout_user
@Bot 踢出李四                    → gm_kick_user
@Bot 拉黑王五，原因是广告         → gm_ban_user
@Bot 警告赵六                    → gm_warn_user
@Bot 撤回张三最近 5 条消息        → gm_recall_user_messages
@Bot 查看当前禁言列表             → （调用 mutelist 命令）
@Bot 把张三设为管理员             → gm_set_admin
@Bot 查询本群信息                → gm_get_group_info
@Bot @所有人 紧急通知             → gm_at_all
@Bot 发送群公告：明天放假         → gm_send_group_notice
```

完整工具列表：`gm_timeout_user` / `gm_kick_user` / `gm_ban_user` / `gm_unban_user` / `gm_warn_user` / `gm_recall_user_messages` / `gm_toggle_whole_mute` / `gm_set_admin` / `gm_status` / `gm_set_warn_max` / `gm_clear_warn` / `gm_get_group_info` / `gm_get_member_info` / `gm_get_member_list` / `gm_get_msg_history` / `gm_at_member` / `gm_at_all` / `gm_set_group_card` / `gm_set_group_essence` / `gm_send_group_notice` / `gm_unmute_user` / `gm_muteall_toggle` / `gm_set_title`

## ⚙️ 配置

插件配置在 WebUI → 插件管理 → astrbot_plugin_groupmaster → 配置：

```yaml
default_mute_seconds: 600        # timeout 未指定秒数时的默认值
max_mute_seconds: 31536000       # timeout 允许的最大秒数（365 天）
warn_max: 3                      # 警告次数上限（可通过 warn max 命令动态修改）
```

## 📝 更新日志

### v1.1.0 (2026-09-06)

**新增功能**
- ✨ 超长禁言支持（最长 365 天，后台自动续期）
- ✨ 新增命令：`unmute` / `mutelist` / `muteall` / `banlist` / `title`
- ✨ 敏感词检测与拦截（`sw` 命令）
- ✨ 刷屏检测与自动处理（`antiflood` 命令）
- ✨ 广告检测与自动拉黑（`ad` / `adban` 命令）
- ✨ 名片检测（`cardcheck` 命令）
- ✨ 入群欢迎与退群提示（`wel` / `bye` 命令）
- ✨ 群公告与群名片/群名修改（`bc` / `g` 命令）
- ✨ 新增 10 个 LLM 工具（总计 21 个）

**优化改进**
- 🔧 事件监听改用 `EventMessageType.ALL` + 内部类型判断（兼容 AstrBot 4.27.4）
- 🔧 后台任务优化（禁言续期 + 定期清理过期记录）
- 📝 完善文档与使用示例

### v1.0.7 (2026-09-05)

初始版本，提供基础群管功能：timeout / kick / ban / unban / warn / recall / mute / admin / status + 跨群批量 + LLM 兜底

## 📄 许可

MIT License

## 🙏 致谢

- 感谢 [ZomebieMask/astrbot_plugin_zm_qqgroupmgr](https://github.com/ZomebieMask/astrbot_plugin_zm_qqgroupmgr) 提供的参考实现
- 感谢 [AstrBot](https://github.com/Soulter/AstrBot) 项目及社区

## 🐛 问题反馈

遇到问题或有功能建议？请在 [GitHub Issues](https://github.com/WolfeOvO/astrbot_plugin_groupmaster/issues) 提交。
