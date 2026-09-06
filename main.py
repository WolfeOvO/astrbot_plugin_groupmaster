"""
AstrBot 群管插件 astrbot_plugin_groupmaster v1.1.0

命令（仅群聊可用；仅本群群主/管理员可使用；群内需 @Bot 或唤醒前缀触发；"@"目标也可以直接输 QQ 号）：
  timeout [all] <秒数> <@用户>   禁言指定用户（秒）；all=机器人管理的所有群
  timeout 0 <@用户>              解除该用户的禁言
  kick  [all] <@用户>            将用户移出群聊
  ban   [all] <@用户> [理由]     移出群聊并拉黑（自动拒绝其再次入群申请），理由存入记录
  unban [all] <@用户>            解除拉黑（允许重新入群）
  warn  [all] <@用户> [理由]     警告次数+1，达到上限自动移出群聊，理由存入记录
  warn  max <次数>               设定全局警告次数上限
  warn  clear <@用户>            清除该用户的警告计数
  （引用一条消息）+ recall        撤回被引用的消息
  recall [all] <条数> <@用户>    撤回该用户最近 N 条消息（允许目标为机器人）
  mute  [all]                    单发一次开关全员禁言（toggle）
  admin set/remove <@用户>       设置/取消群管理员（需机器人为群主）
  status [/@用户/QQ号]           查询本群禁言剩余/警告计数/拉黑名单与理由；带目标=查该用户档案
  @Bot + 自然语言                未命中命令时走 LLM，调用本插件注册的 llm_tool 完成同套操作
                                  工具集 20 个（11 管理类 + 9 信息/互动类），详见 README
                                  LLM 输出 [at:QQ] / [at:all] 标签由本插件转原生 At 组件

OneBot(NapCat) 动作：set_group_ban / set_group_kick / delete_msg /
set_group_whole_ban / set_group_admin / set_group_add_request /
get_group_member_info / get_group_list / get_group_msg_history / get_group_setting /
get_group_shut_list / set_essence / set_group_card / send_group_notice
"""

import json
import logging
import os
import re
import time
from typing import List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.message_components import At, BaseMessageComponent, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")
LOG = "[groupmaster]"

# LLM 自然语言兜底：在 LLM 输出文本中插入 [at:QQ号] / [at:all] 标签
AT_PATTERN = re.compile(r"\[at:(?P<uid>\d+|all)\]")
AT_INSTRUCTION = (
    "\n\n【艾特成员提示】"
    "\n当你想在回复中 @ 某个群成员时，请在文本中插入格式 [at:用户ID] 的标签（ID 必须是纯数字 QQ 号）。"
    "\n要 @ 全体成员，插入 [at:all]。"
    "\n例：你好[at:123456789]，已处理你的问题。"
    "\n如需先查 QQ 号，可调用 gm_get_member_list 工具。"
)

# 超长禁言分段参数（QQ 单次最多 30 天 = 2592000 秒）
MUTE_CHUNK = 2592000  # 30天
RENEW_LEAD = 300      # 提前 5 分钟续期
MAX_MUTE_DAYS = 365   # 超长禁言上限（天）

# 敏感词 / 刷屏 / 广告 默认阈值
DEFAULT_SW_DURATION = 600
DEFAULT_FLOOD_THRESHOLD = 5
DEFAULT_FLOOD_WINDOW = 10
DEFAULT_FLOOD_REPEAT = 3
DEFAULT_AD_THRESHOLD = 10

# LLM 自然语言兜底：在 LLM 输出文本中插入 [at:QQ号] / [at:all] 标签，
# 由 on_decorating_result 阶段解析为原生 At 组件。
AT_PATTERN = re.compile(r"\[at:(?P<uid>\d+|all)\]")
AT_INSTRUCTION = (
    "\n\n【艾特成员提示】"
    "\n当你想在回复中 @ 某个群成员时，请在文本中插入格式 [at:用户ID] 的标签（ID 必须是纯数字 QQ 号）。"
    "\n要 @ 全体成员，插入 [at:all]。"
    "\n例：你好[at:123456789]，已处理你的问题。"
    "\n例：@全体成员[at:all] 注意 5 分钟后开始。"
    "\n如需先查 QQ 号，可调用 gm_get_member_list 工具。"
)

COMMAND_NAMES = {"timeout", "kick", "ban", "unban", "warn", "recall", "mute", "muteall", "unmute", "mutelist", "admin", "status", "banlist", "title", "sw", "antiflood", "cardcheck", "ad", "adban", "wel", "bye", "bc", "g"}
OP_NAMES = {
    "timeout": "禁言",
    "unmute": "解禁",
    "mutelist": "禁言列表",
    "muteall": "全员禁言(定时)",
    "kick": "踢出",
    "ban": "拉黑踢出",
    "unban": "解除拉黑",
    "banlist": "拉黑列表",
    "warn": "警告",
    "recall": "撤回",
    "mute": "全员禁言开关",
    "admin": "管理员设置",
    "status": "状态查询",
    "title": "头衔",
    "sw": "敏感词",
    "antiflood": "刷屏检测",
    "cardcheck": "名片检测",
    "ad": "广告检测",
    "adban": "广告拦截",
    "wel": "欢迎消息",
    "bye": "退群提示",
    "bc": "群公告",
    "g": "群操作",
}
# @昵称(QQ号) / @(QQ号)（空昵称）/ @任意字符(QQ号)
AT_RE = re.compile(r"@\S*?\((\d{5,12})\)")


def parse_command_tokens(message_str: str) -> Tuple[str, list]:
    """从 message_str 提取 (命令名, 参数token列表)。容忍唤醒前缀等残留。"""
    toks = [t for t in (message_str or "").split() if t]
    while toks:
        head = toks[0].lower().lstrip("#＃/!！ ")
        if head in COMMAND_NAMES:
            return head, toks[1:]
        if AT_RE.search(toks[0]) or toks[0].startswith(("#", "＃", "/", "!")):
            toks = toks[1:]
            continue
        break
    return "", []


def first_int(tokens: list) -> Optional[int]:
    """取第一个纯数字 token（跳过 @昵称(QQ号) 等非数字 token）。"""
    for t in tokens or []:
        if t.isdigit():
            return int(t)
    return None


def extract_target_qq(event: AstrMessageEvent, tokens: list, numeric_param_first: bool = False, allow_self: bool = False) -> str:
    """定位目标 QQ。优先级：消息链 At 段（默认排除 bot 自身/全体）> 文本 @昵称(QQ号) > 裸数字 token。

    numeric_param_first=True 表示第一个纯数字 token 是时间/条数参数而非目标
    （timeout/recall 命令里时间在前、目标在后）。
    allow_self=True 时，若链中只 @ 了机器人自身且无其他目标，返回机器人 QQ
    （供撤回机器人自己的消息等场景使用）。
    """
    self_id = ""
    try:
        self_id = str(event.get_self_id() or "")
    except Exception:
        pass
    # 1) 消息链中的 At 组件
    self_seen = ""
    try:
        for comp in getattr(event.message_obj, "message", None) or []:
            qq = str(getattr(comp, "qq", "") or "")
            if not qq:
                continue
            if qq == "all" or qq.startswith("remove"):
                continue
            if qq == self_id:
                if allow_self and not self_seen:
                    self_seen = qq
                continue
            return qq
    except Exception:
        pass
    if self_seen:
        return self_seen
    # 2) 文本中的 @昵称(QQ号)
    m = AT_RE.search(getattr(event, "message_str", "") or "")
    if m:
        return m.group(1)
    # 3) 裸数字 token
    nums = [t for t in tokens or [] if t.isdigit()]
    if numeric_param_first:
        nums = nums[1:] if len(nums) > 1 else []
    return nums[-1] if nums else ""


def extract_reason(tokens: list, last_target: str = "") -> str:
    """取理由：目标（@昵称(QQ号) 或裸 QQ 号）token 之后的剩余文本，整体拼接。

    last_target 是解析出的目标 QQ 号，用于在裸 QQ 号形式下定位起点。
    """
    toks = [t for t in tokens or [] if t]
    start = 0
    for i, t in enumerate(toks):
        m = AT_RE.search(t)
        if m and m.group(1):
            start = i + 1
            break
        if last_target and t == last_target:
            start = i + 1
            break
    else:
        # 没找到目标 token：跳过开头的命令残留（如 warn/ban/clear/max/数字），其余当理由
        start = 0
    reason = " ".join(toks[start:]).strip()
    return reason


def chain_has_at_self(event: AstrMessageEvent) -> bool:
    """消息链中是否 @ 了机器人自身（第一个 @ 机器人不进 message_str，只能查链）。"""
    try:
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return False
        for comp in getattr(event.message_obj, "message", None) or []:
            if str(getattr(comp, "qq", "") or "") == self_id:
                return True
    except Exception:
        pass
    return False


def find_reply_id(event: AstrMessageEvent) -> Optional[str]:
    """取引用消息 ID：Reply 是消息链中的组件（aiocqhttp 适配器把 reply 段 append 进 message）。"""
    try:
        for comp in getattr(event.message_obj, "message", None) or []:
            tname = type(comp).__name__
            ctype = str(getattr(comp, "type", "") or "")
            if tname == "Reply" or ctype == "Reply":
                rid = getattr(comp, "id", None)
                if rid:
                    return str(rid)
    except Exception:
        pass
    # 兼容其他适配器可能挂载在属性上的情况
    reply = getattr(event.message_obj, "reply", None)
    if reply is not None:
        rid = getattr(reply, "id", None)
        if rid:
            return str(rid)
    return None


@register(
    "astrbot_plugin_groupmaster",
    "Wolfe",
    "QQ群管插件：timeout/kick/ban/warn/recall/mute/admin/status，支持@或QQ号定位、理由记录、跨群 all 批量执行与 LLM 自然语言兜底；仅群聊可用，仅本群群主/管理员可使用。",
    "1.1.0",
    "",
)
class GroupMasterPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = os.path.join(get_astrbot_plugin_data_path(), "astrbot_plugin_groupmaster")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"{LOG} 创建数据目录失败: {e}")
        self._tasks = set()  # 后台任务集合

        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state = self._load_state()
        self._whole_mute_mem = {}

    # ---------------- 状态持久化 ----------------
    def _load_state(self) -> dict:
        default_warn_max = 3
        try:
            default_warn_max = int(self.config.get("warn_max", 3) or 3)
        except Exception:
            pass
        state = {"warn_max": default_warn_max, "warns": {}, "bans": {}}
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    state.update({k: loaded[k] for k in state if k in loaded})
        except Exception as e:
            logger.warning(f"{LOG} 读取状态文件失败，使用默认值: {e}")
        # 结构兜底
        if not isinstance(state.get("warns"), dict):
            state["warns"] = {}
        if not isinstance(state.get("bans"), dict):
            state["bans"] = {}
        if not isinstance(state.get("reasons"), dict):
            state["reasons"] = {}
        return state

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.error(f"{LOG} 保存状态文件失败: {e}")

    # ---------------- OneBot 调用与权限 ----------------
    async def _ob(self, event: AstrMessageEvent, action: str, **kw):
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前事件未关联 OneBot 平台实例（仅支持 aiocqhttp/NapCat 适配器）")
        if hasattr(bot, "call_action"):
            return await bot.call_action(action, **kw)
        api = getattr(bot, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return await api.call_action(action, **kw)
        if hasattr(bot, "call_api"):
            return await bot.call_api(action, **kw)
        raise RuntimeError("bot 实例不支持 OneBot action 调用")

    def _is_group(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:
            return False

    async def _role_of(self, event: AstrMessageEvent, group_id, user_id) -> str:
        try:
            res = await self._ob(
                event, "get_group_member_info",
                group_id=int(group_id), user_id=int(user_id), no_cache=True,
            )
            data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
            if isinstance(data, dict):
                return str(data.get("role", "") or "")
        except Exception as e:
            logger.warning(f"{LOG} get_group_member_info 失败: {e}")
        return ""

    async def _authorized(self, event: AstrMessageEvent) -> bool:
        """权限门：仅本群群主/管理员可使用（get_group_member_info 实时校验群角色，查询失败即拒绝）。"""
        if not self._is_group(event):
            return False
        role = await self._role_of(event, event.get_group_id(), event.get_sender_id())
        return role in ("admin", "owner")

    async def _bot_gate(self, event: AstrMessageEvent, gid, need_owner: bool = False) -> Tuple[bool, str]:
        role = await self._role_of(event, gid, event.get_self_id())
        if need_owner:
            if role != "owner":
                return False, "该操作需要机器人为群主（当前角色: %s）" % (role or "member/未知")
            return True, ""
        if role not in ("admin", "owner"):
            return False, "机器人需要群管理员权限才能执行该操作（当前角色: %s）" % (role or "member/未知")
        return True, ""

    def _gated(self, event: AstrMessageEvent) -> Optional[str]:
        """私聊禁用检查。"""
        try:
            if event.is_private_chat() or not self._is_group(event):
                return "该命令仅能在群聊中使用。"
        except Exception:
            return "该命令仅能在群聊中使用。"
        return None

    # ---------------- 群定位辅助 ----------------
    async def _global_groups(self, event: AstrMessageEvent) -> list:
        """机器人担任管理员或群主的所有群 [(gid, role)]。"""
        res = await self._ob(event, "get_group_list")
        data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), list) else res
        groups = data if isinstance(data, list) else []
        result = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("group_id", "") or "")
            if not gid:
                continue
            role = await self._role_of(event, gid, event.get_self_id())
            if role in ("admin", "owner"):
                result.append((gid, role))
        return result

    async def _get_whole_ban(self, event: AstrMessageEvent, gid) -> bool:
        """查当前全员禁言状态：优先 NapCat get_group_setting，失败退回内存记录。"""
        try:
            res = await self._ob(event, "get_group_setting", group_id=int(gid))
            data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
            if isinstance(data, dict):
                for k in ("whole_ban", "is_ban", "wholeBan", "isWholeBan"):
                    if k in data:
                        return bool(data[k])
        except Exception:
            pass
        return self._whole_mute_mem.get(str(gid), False)

    async def _collect_target_msgs(self, event: AstrMessageEvent, gid, target: str, want: int, exclude_ids: set) -> list:
        """从群历史消息（最新往旧翻页）收集 target 的消息 ID，最多 want 条。"""
        found = []
        exclude_strs = {str(x) for x in exclude_ids}
        scanned = 0
        limit = 100
        try:
            limit = int(self.config.get("recall_scan_limit", 100) or 100)
        except Exception:
            pass
        seq = None
        seen_seqs = set()
        for _ in range(12):
            kw = {"group_id": int(gid)}
            if seq:
                kw["message_seq"] = seq
            try:
                res = await self._ob(event, "get_group_msg_history", **kw)
            except Exception as e:
                logger.warning(f"{LOG} get_group_msg_history 失败: {e}")
                break
            data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
            msgs = data.get("messages") if isinstance(data, dict) else None
            if not msgs or not isinstance(msgs, list):
                break
            scanned += len(msgs)
            min_seq = None
            for m in reversed(msgs):
                if not isinstance(m, dict):
                    continue
                mid = m.get("message_id")
                suid = str((m.get("sender") or {}).get("user_id", "") or "")
                if mid is not None and str(mid) not in exclude_strs and suid == target:
                    found.append(int(mid))
                    if len(found) >= want:
                        return found
                msq = m.get("message_seq")
                if isinstance(msq, int):
                    min_seq = msq if min_seq is None else min(min_seq, msq)
            if min_seq is None or (seq is not None and min_seq >= seq) or min_seq in seen_seqs:
                break
            seen_seqs.add(min_seq)
            seq = min_seq
            if scanned >= limit:
                break
        return found

    # ---------------- 单群执行（供命令与全局批量化复用） ----------------
    async def _do_timeout(self, event, gid, target: str, dur: int, reason: str = "") -> Tuple[bool, str]:
        if target == str(event.get_self_id()):
            return False, "不能对机器人自己执行该操作"
        ok, msg = await self._bot_gate(event, gid)
        if not ok:
            return False, msg
        try:
            await self._ob(event, "set_group_ban", group_id=int(gid), user_id=int(target), duration=dur)
        except Exception as e:
            return False, f"禁言失败: {e}"
        # 本地禁言记账：记录到期时间戳，供 status 兜底（NapCat get_group_shut_list
        # 基于 1 秒内核事件监听，可能漏报正在禁言中的成员）
        try:
            memo = self.state.setdefault("mute_memo", {}).setdefault(str(gid), {})
            if dur > 0:
                memo[str(target)] = int(time.time()) + int(dur)
            else:
                memo.pop(str(target), None)
            # 顺手清理已过期条目
            now = int(time.time())
            for uid in [u for u, exp in memo.items() if int(exp) <= now]:
                memo.pop(uid, None)
            # 禁言理由：写入 reasons，schema 与 ban/warn 对齐
            rk = f"timeout:{target}"
            rs = self.state.setdefault("reasons", {}).setdefault(str(gid), {})
            if dur > 0:
                rs[rk] = {"t": int(time.time()), "r": str(reason or "")[:100], "exp": int(time.time()) + int(dur)}
            else:
                # 解除禁言时清掉对应理由
                rs.pop(rk, None)
            self._save_state()
        except Exception:
            pass
        if dur <= 0:
            return True, f"已解除 {target} 的禁言"
        return True, f"已禁言 {target} {dur} 秒" + (f"｜理由：{reason}" if reason else "")

    async def _do_kick(self, event, gid, target: str, blacklist: bool, reason: str = "") -> Tuple[bool, str]:
        if target == str(event.get_self_id()):
            return False, "不能对机器人自己执行该操作"
        ok, msg = await self._bot_gate(event, gid)
        if not ok:
            return False, msg
        try:
            await self._ob(
                event, "set_group_kick",
                group_id=int(gid), user_id=int(target), reject_add_request=bool(blacklist),
            )
            if blacklist:
                self.state.setdefault("bans", {}).setdefault(str(gid), {})[str(target)] = int(time.time())
                try:
                    self.state.setdefault("reasons", {}).setdefault(str(gid), {})[f"ban:{target}"] = {"t": int(time.time()), "r": reason[:100]}
                except Exception:
                    pass
                self._save_state()
                return True, f"已将 {target} 移出群聊并拉黑（后续入群申请将被自动拒绝）" + (f"｜理由：{reason}" if reason else "")
            # 普通踢出（不拉黑）：reason 写入 reasons[kick:uid]，不写 bans
            try:
                self.state.setdefault("reasons", {}).setdefault(str(gid), {})[f"kick:{target}"] = {"t": int(time.time()), "r": str(reason or "")[:100]}
                self._save_state()
            except Exception:
                pass
            return True, f"已将 {target} 移出群聊" + (f"｜理由：{reason}" if reason else "")
        except Exception as e:
            return False, f"踢出失败: {e}"

    async def _do_unban(self, event, gid, target: str) -> Tuple[bool, str]:
        bans = self.state.get("bans", {}).get(str(gid), {})
        if str(target) not in bans:
            return False, f"{target} 不在本群拉黑名单中"
        del bans[str(target)]
        self._save_state()
        return True, f"已解除 {target} 的拉黑（可重新申请入群）"

    async def _do_warn(self, event, gid, target: str, reason: str = "") -> Tuple[bool, str]:
        if target == str(event.get_self_id()):
            return False, "不能对机器人自己执行该操作"
        warns = self.state.setdefault("warns", {}).setdefault(str(gid), {})
        count = int(warns.get(str(target), 0)) + 1
        warn_max = int(self.state.get("warn_max", 3) or 3)
        # 理由与时间随计数一起持久化（status 查询可见；最多留最近 3 条）
        try:
            recs = self.state.setdefault("reasons", {}).setdefault(str(gid), {}).setdefault(f"warn:{target}", [])
            recs.append({"t": int(time.time()), "r": reason[:100]})
            self.state["reasons"][str(gid)][f"warn:{target}"] = recs[-3:]
        except Exception:
            pass
        if count >= warn_max:
            ok, msg = await self._do_kick(event, gid, target, blacklist=False)
            if ok:
                warns[str(target)] = 0
                self._save_state()
                return True, f"警告 {target} {count}/{warn_max} 已达上限，已移出群聊" + (f"（理由：{reason}）" if reason else "")
            warns[str(target)] = count
            self._save_state()
            return False, f"警告计数已存（{count}/{warn_max}），但移出失败: {msg}"
        warns[str(target)] = count
        self._save_state()
        return True, f"已警告 {target}（{count}/{warn_max}）" + (f"｜理由：{reason}" if reason else "")

    async def _do_recall(self, event, gid, toks: list) -> Tuple[bool, str]:
        # 1) 引用撤回（优先）：Reply 是消息链中的组件
        rid = find_reply_id(event)
        if rid:
            try:
                await self._ob(event, "delete_msg", message_id=int(rid))
                return True, "已撤回引用的那条消息"
            except Exception as e:
                return False, f"撤回引用消息失败: {e}"
        # 2) 按条数撤回指定用户（允许目标是机器人自己，撤回 bot 的消息是合法的）
        count = first_int(toks) or 1
        target = extract_target_qq(event, toks, numeric_param_first=True, allow_self=True)
        if not target:
            return False, "用法：引用消息 + recall；或 recall <条数> <@用户/QQ号>"
        ok, msg = await self._bot_gate(event, gid)
        if not ok:
            return False, msg
        exclude = {str(getattr(event.message_obj, "message_id", "") or "")}
        want = max(1, min(count, 50))
        try:
            mids = await self._collect_target_msgs(event, gid, target, want, exclude)
        except Exception as e:
            return False, f"获取群历史消息失败: {e}"
        if not mids:
            return False, f"在近期历史消息中未找到 {target} 的可撤回消息"
        done, first_err = 0, ""
        for mid in mids:
            try:
                await self._ob(event, "delete_msg", message_id=int(mid))
                done += 1
            except Exception as e:
                if not first_err:
                    first_err = str(e)
        extra = f"（{len(mids) - done} 条失败: {first_err[:80]}）" if first_err else ""
        return (done > 0), f"已撤回 {target} 的 {done}/{len(mids)} 条消息{extra}"

    async def _do_mute(self, event, gid) -> Tuple[bool, str]:
        ok, msg = await self._bot_gate(event, gid)
        if not ok:
            return False, msg
        cur = await self._get_whole_ban(event, gid)
        new = not cur
        try:
            await self._ob(event, "set_group_whole_ban", group_id=int(gid), enable=new)
            self._whole_mute_mem[str(gid)] = new
            return True, f"已{'开启' if new else '关闭'}全员禁言"
        except Exception as e:
            return False, f"切换全员禁言失败: {e}"

    def _fmt_ts(self, ts) -> str:
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
        except Exception:
            return "?"

    def _shut_map(self, res) -> dict:
        """归一化 get_group_shut_list 返回 → {uid: 到期时间戳}。

        NapCat 返回数组 [{user_id, nickname, shut_up_time}, ...]（shut_up_time 实测为禁言截止
        时间戳；若整表均早于当前时间，则按禁言起点处理，补 default_mute_seconds 推算到期）；
        其他实现可能返回字典 {uid: 到期时间戳} 或 {uid: {"b": ...}}。无法识别时返回空 dict。
        """
        data = res.get("data") if isinstance(res, dict) else res
        out: dict = {}
        now = int(time.time())
        if isinstance(data, list):
            raw = {}
            for it in data:
                if not isinstance(it, dict):
                    continue
                uid = str(it.get("user_id") or it.get("u") or it.get("uid") or "")
                ts = it.get("shut_up_time", it.get("b", it.get("end_time", 0)))
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    continue
                if uid and uid.isdigit() and ts > 0:
                    raw[uid] = ts
            if raw and max(raw.values()) > now:
                # 存在晚于当前时间的条目 → 字段是截止时间，只保留仍在禁言中的
                out = {uid: ts for uid, ts in raw.items() if ts > now}
            else:
                # 全部早于当前时间 → 字段是禁言起点，补默认时长推算到期
                try:
                    dur = int(self.state.get("default_mute_seconds", 600) or 600)
                except Exception:
                    dur = 600
                out = {uid: ts + dur for uid, ts in raw.items() if ts + dur > now}
        elif isinstance(data, dict):
            for uid, info in data.items():
                try:
                    ts = int(info.get("b") if isinstance(info, dict) else info)
                except (TypeError, ValueError):
                    continue
                if ts > now:
                    out[str(uid)] = ts
        return out

    async def _do_status(self, event, gid, target: str = "") -> Tuple[bool, str]:
        """本群状态查询：谁被禁言/禁言剩余时间、警告计数、拉黑名单与理由。仅同群群主/管理员可用。"""
        lines = []
        # 单人模式：status <@用户/QQ号> → 该用户在本群的完整管理档案
        if target:
            role = ""
            card = ""
            try:
                res = await self._ob(event, "get_group_member_info", group_id=int(gid), user_id=int(target), no_cache=True)
                data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
                if isinstance(data, dict):
                    role = str(data.get("role", "") or "")
                    card = str(data.get("card", "") or data.get("nickname", "") or "")
            except Exception:
                pass
            role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role or "未知/已不在群")
            lines.append(f"👤 {target}（{card or '无群名片'}）｜群身份：{role_cn}")
            warns = self.state.get("warns", {}).get(str(gid), {})
            warn_max = int(self.state.get("warn_max", 3) or 3)
            w = int(warns.get(str(target), 0) or 0)
            lines.append(f"⚠️ 警告：{w}/{warn_max}")
            wrecs = self.state.get("reasons", {}).get(str(gid), {}).get(f"warn:{target}", [])
            for rec in wrecs[-3:]:
                lines.append(f"   📝 {self._fmt_ts(rec.get('t'))}｜{rec.get('r', '') or '（未附理由）'}")
            if str(target) in self.state.get("bans", {}).get(str(gid), {}):
                b = self.state.get("bans", {})[str(gid)][str(target)]
                br = self.state.get("reasons", {}).get(str(gid), {}).get(f"ban:{target}", {}).get("r", "")
                lines.append(f"🚫 已拉黑（{self._fmt_ts(b)}{'｜' + br if br else ''}），其入群申请会被自动拒绝")
            muted = False
            try:
                shut = self._shut_map(await self._ob(event, "get_group_shut_list", group_id=int(gid)))
                shut = {**self.state.get("mute_memo", {}).get(str(gid), {}), **shut}
                remain = int(shut.get(str(target), 0)) - int(time.time())
                if remain > 0:
                    h, m2, s = remain // 3600, (remain % 3600) // 60, remain % 60
                    t = (f"{h}时" if h else "") + (f"{m2}分" if h or m2 else "") + f"{s}秒"
                    tr = self.state.get("reasons", {}).get(str(gid), {}).get(f"timeout:{target}", {}).get("r", "")
                    lines.append(f"🔇 禁言中：剩余 {t}" + (f"｜理由：{tr}" if tr else ""))
                    muted = True
            except Exception:
                pass
            if not muted and role:
                lines.append("🔇 禁言：无")
            return True, "\n".join(lines)
        # 全群总览
        # 1) 禁言列表（get_group_shut_list：NapCat 扩展，失败则提示不可查）
        try:
            shut = self._shut_map(await self._ob(event, "get_group_shut_list", group_id=int(gid)))
            shut = {**self.state.get("mute_memo", {}).get(str(gid), {}), **shut}
            now = int(time.time())
            cnt = 0
            for uid, b in shut.items():
                remain = int(b) - now
                if remain > 0:
                    h, m2, s = remain // 3600, (remain % 3600) // 60, remain % 60
                    t = (f"{h}时" if h else "") + (f"{m2}分" if h or m2 else "") + f"{s}秒"
                    tr = self.state.get("reasons", {}).get(str(gid), {}).get(f"timeout:{uid}", {}).get("r", "")
                    lines.append(f"🔇 {uid} 禁言剩余 {t}" + (f"｜理由：{tr}" if tr else ""))
                    cnt += 1
                    if cnt >= 20:
                        break
            if cnt == 0:
                lines.append("🔇 当前无人处于禁言中")
        except Exception as e:
            lines.append(f"🔇 禁言列表不可查（{str(e)[:60]}）")
        # 2) 警告计数
        warns = self.state.get("warns", {}).get(str(gid), {})
        warn_max = int(self.state.get("warn_max", 3) or 3)
        if warns:
            warn_line = "｜".join(f"{uid}:{c}/{warn_max}" for uid, c in list(warns.items())[:20] if int(c or 0) > 0)
            lines.append(f"⚠️ 警告记录：{warn_line if warn_line else '无在记用户'}")
        else:
            lines.append("⚠️ 警告记录：无")
        # 3) 拉黑名单
        bans = self.state.get("bans", {}).get(str(gid), {})
        if bans:
            bparts = []
            for uid, ts in list(bans.items())[:20]:
                reason = self.state.get("reasons", {}).get(str(gid), {}).get(f"ban:{uid}", {}).get("r", "")
                bparts.append(f"{uid}（{self._fmt_ts(ts)}{'｜' + reason if reason else ''}）")
            lines.append("🚫 拉黑：" + "、".join(bparts))
        else:
            lines.append("🚫 拉黑：无")
        # 4) 最近警告理由
        reasons = self.state.get("reasons", {}).get(str(gid), {})
        wreasons = [(k, v) for k, v in reasons.items() if k.startswith("warn:") and v]
        if wreasons:
            parts = []
            for k, v in wreasons[:20]:
                uid = k[5:]
                last = v[-1]
                parts.append(f"{uid}（{self._fmt_ts(last.get('t'))}｜{last.get('r', '')}）")
            lines.append("📝 最近警告理由：" + "、".join(parts))
        # 5) 最近踢出理由（普通踢出 + 拉黑踢出合并展示，ban:* 优先）
        krecs = [(k, v) for k, v in reasons.items() if k.startswith("kick:") and v and v.get("r")]
        if krecs:
            parts = []
            for k, v in krecs[:20]:
                uid = k[5:]
                parts.append(f"{uid}（{self._fmt_ts(v.get('t'))}｜{v.get('r', '')}）")
            lines.append("🥾 最近踢出理由：" + "、".join(parts))
        return True, "\n".join(lines)

    async def _do_admin(self, event, gid, toks: list) -> Tuple[bool, str]:
        sub = (toks[0].lower() if toks else "")
        if sub not in ("set", "remove"):
            return False, "用法：admin set/remove <@用户/QQ号>"
        target = extract_target_qq(event, toks[1:], numeric_param_first=False)
        if not target:
            return False, "缺少目标用户"
        ok, msg = await self._bot_gate(event, gid, need_owner=True)
        if not ok:
            return False, msg
        try:
            await self._ob(
                event, "set_group_admin",
                group_id=int(gid), user_id=int(target), enable=(sub == "set"),
            )
            return True, f"已{'设置' if sub == 'set' else '取消'} {target} 的群管理员"
        except Exception as e:
            return False, f"设置管理员失败: {e}"


    async def _do_unmute(self, event, gid, target: str) -> Tuple[bool, str]:
        """解除禁言（等价于 timeout 0）"""
        if target == str(event.get_self_id()):
            return False, "不能对机器人自己解禁"
        try:
            ob = self._ob(event)
            await ob.call("set_group_ban", group_id=int(gid), user_id=int(target), duration=0)
            # 清理超长禁言记录
            memo = self.state.get("mute_memo", {}).get(str(gid), {})
            if str(target) in memo:
                del memo[str(target)]
                self._save_state()
            return True, f"已解除 {target} 的禁言"
        except Exception as e:
            return False, f"解除禁言失败: {e}"

    async def _do_muteall(self, event, gid, dur: int) -> Tuple[bool, str]:
        """全员禁言指定时长（秒），0=关闭"""
        try:
            ob = self._ob(event)
            if dur == 0:
                await ob.call("set_group_whole_ban", group_id=int(gid), enable=False)
                return True, "已关闭全员禁言"
            else:
                await ob.call("set_group_whole_ban", group_id=int(gid), enable=True)
                # 注意：QQ 的全员禁言没有自动到期，需手动关闭或用定时任务
                return True, f"已开启全员禁言（需手动关闭或设定时任务 {dur}秒后关闭）"
        except Exception as e:
            return False, f"全员禁言操作失败: {e}"

    async def _do_mutelist(self, event, gid) -> Tuple[bool, str]:
        """列出当前群所有被禁言的成员"""
        try:
            ob = self._ob(event)
            members = await ob.call("get_group_member_list", group_id=int(gid))
            if not members:
                return False, "获取成员列表失败"
            
            import time
            now = int(time.time())
            muted = []
            for m in members:
                shut = m.get("shut_up_timestamp", 0)
                if shut and shut > now:
                    remaining = shut - now
                    name = m.get("card") or m.get("nickname") or str(m.get("user_id"))
                    muted.append(f"{name}({m['user_id']}) 剩余{remaining//60}分{remaining%60}秒")
            
            if not muted:
                return True, "当前群没有被禁言的成员"
            return True, f"禁言列表({len(muted)}):\n" + "\n".join(muted[:20])
        except Exception as e:
            return False, f"查询禁言列表失败: {e}"

    async def _do_banlist(self, event, gid) -> Tuple[bool, str]:
        """列出当前群拉黑名单"""
        bans = self.state.get("bans", {}).get(str(gid), {})
        if not bans:
            return True, "当前群拉黑名单为空"
        lines = []
        for uid, rec in list(bans.items())[:20]:
            reason = rec.get("reason", "")
            ts = rec.get("timestamp", 0)
            import time
            date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
            lines.append(f"{uid} {date} {reason}"[:80])
        return True, f"拉黑名单({len(bans)}):\n" + "\n".join(lines)

    async def _do_title(self, event, gid, toks: list) -> Tuple[bool, str]:
        """设置群成员头衔：title <@用户> <头衔文本>"""
        target = extract_target_qq(event, toks)
        if not target:
            return False, "用法：title <@用户> <头衔文本>"
        title_text = " ".join([t for t in toks if not AT_RE.search(t) and not t.isdigit()])
        if not title_text:
            return False, "请提供头衔文本"
        try:
            ob = self._ob(event)
            await ob.call("set_group_special_title", group_id=int(gid), user_id=int(target), special_title=title_text, duration=-1)
            return True, f"已设置 {target} 的头衔为 {title_text}"
        except Exception as e:
            return False, f"设置头衔失败: {e}"

    async def _do_sw(self, event, gid, toks: list) -> Tuple[bool, str]:
        """敏感词管理：sw add/del/list/on/off/set"""
        if not toks:
            return False, "用法：sw add <词> | sw del <词> | sw list | sw on/off | sw set <秒数>"
        sub = toks[0].lower()
        sw_conf = self.state.setdefault("sw", {}).setdefault(str(gid), {"enabled": False, "words": [], "duration": DEFAULT_SW_DURATION})
        
        if sub == "on":
            sw_conf["enabled"] = True
            self._save_state()
            return True, "已开启敏感词检测"
        elif sub == "off":
            sw_conf["enabled"] = False
            self._save_state()
            return True, "已关闭敏感词检测"
        elif sub == "add":
            word = " ".join(toks[1:]).strip()
            if not word:
                return False, "用法：sw add <敏感词>"
            if word not in sw_conf["words"]:
                sw_conf["words"].append(word)
                self._save_state()
            return True, f"已添加敏感词：{word}"
        elif sub == "del":
            word = " ".join(toks[1:]).strip()
            if word in sw_conf["words"]:
                sw_conf["words"].remove(word)
                self._save_state()
                return True, f"已删除敏感词：{word}"
            return False, f"敏感词不存在：{word}"
        elif sub == "list":
            words = sw_conf.get("words", [])
            status = "开启" if sw_conf.get("enabled") else "关闭"
            dur = sw_conf.get("duration", DEFAULT_SW_DURATION)
            if not words:
                return True, f"敏感词检测状态：{status}，词库为空，禁言时长{dur}秒"
            return True, f"敏感词检测({status})，禁言{dur}秒，词库({len(words)})：\n" + "\n".join(words[:30])
        elif sub == "set":
            dur = first_int(toks[1:])
            if dur is None or dur < 0:
                return False, "用法：sw set <秒数>"
            sw_conf["duration"] = min(dur, 2592000)
            self._save_state()
            return True, f"已设置敏感词禁言时长为 {dur} 秒"
        return False, "未知子命令"

    async def _do_antiflood(self, event, gid, toks: list) -> Tuple[bool, str]:
        """刷屏检测：antiflood on/off/set"""
        if not toks:
            return False, "用法：antiflood on/off | antiflood set <阈值> <窗口秒>"
        sub = toks[0].lower()
        flood_conf = self.state.setdefault("flood", {}).setdefault(str(gid), {"enabled": False, "threshold": DEFAULT_FLOOD_THRESHOLD, "window": DEFAULT_FLOOD_WINDOW})
        
        if sub == "on":
            flood_conf["enabled"] = True
            self._save_state()
            return True, "已开启刷屏检测"
        elif sub == "off":
            flood_conf["enabled"] = False
            self._save_state()
            return True, "已关闭刷屏检测"
        elif sub == "set":
            nums = [int(t) for t in toks[1:] if t.isdigit()]
            if len(nums) < 2:
                return False, "用法：antiflood set <阈值> <窗口秒>"
            flood_conf["threshold"] = nums[0]
            flood_conf["window"] = nums[1]
            self._save_state()
            return True, f"已设置刷屏阈值：{nums[1]}秒内{nums[0]}条消息"
        return False, "未知子命令"

    async def _do_cardcheck(self, event, gid, toks: list) -> Tuple[bool, str]:
        """名片检测：cardcheck on/off"""
        if not toks:
            return False, "用法：cardcheck on/off"
        sub = toks[0].lower()
        card_conf = self.state.setdefault("cardcheck", {}).setdefault(str(gid), {"enabled": False})
        
        if sub == "on":
            card_conf["enabled"] = True
            self._save_state()
            return True, "已开启名片检测（入群时检查）"
        elif sub == "off":
            card_conf["enabled"] = False
            self._save_state()
            return True, "已关闭名片检测"
        return False, "未知子命令"

    async def _do_ad(self, event, gid, toks: list) -> Tuple[bool, str]:
        """广告检测配置：ad on/off/set"""
        if not toks:
            return False, "用法：ad on/off | ad set <阈值>"
        sub = toks[0].lower()
        ad_conf = self.state.setdefault("ad", {}).setdefault(str(gid), {"enabled": False, "threshold": DEFAULT_AD_THRESHOLD})
        
        if sub == "on":
            ad_conf["enabled"] = True
            self._save_state()
            return True, "已开启广告检测"
        elif sub == "off":
            ad_conf["enabled"] = False
            self._save_state()
            return True, "已关闭广告检测"
        elif sub == "set":
            thr = first_int(toks[1:])
            if thr is None:
                return False, "用法：ad set <阈值>"
            ad_conf["threshold"] = thr
            self._save_state()
            return True, f"已设置广告评分阈值为 {thr}"
        return False, "未知子命令"

    async def _do_adban(self, event, gid, toks: list) -> Tuple[bool, str]:
        """广告拦截行为：adban mute/kick/ban"""
        if not toks:
            return False, "用法：adban mute/kick/ban"
        action = toks[0].lower()
        if action not in ("mute", "kick", "ban"):
            return False, "未知动作，可选：mute/kick/ban"
        ad_conf = self.state.setdefault("ad", {}).setdefault(str(gid), {})
        ad_conf["action"] = action
        self._save_state()
        return True, f"已设置广告拦截动作为 {action}"

    async def _do_wel(self, event, gid, toks: list) -> Tuple[bool, str]:
        """欢迎消息：wel on/off/set <消息>"""
        if not toks:
            return False, "用法：wel on/off | wel set <消息文本>"
        sub = toks[0].lower()
        wel_conf = self.state.setdefault("wel", {}).setdefault(str(gid), {"enabled": False, "msg": "欢迎 {user} 加入本群！"})
        
        if sub == "on":
            wel_conf["enabled"] = True
            self._save_state()
            return True, "已开启入群欢迎"
        elif sub == "off":
            wel_conf["enabled"] = False
            self._save_state()
            return True, "已关闭入群欢迎"
        elif sub == "set":
            msg = " ".join(toks[1:]).strip()
            if not msg:
                return False, "用法：wel set <消息文本>（可用 {user} 占位符）"
            wel_conf["msg"] = msg
            self._save_state()
            return True, f"已设置欢迎消息：{msg}"
        return False, "未知子命令"

    async def _do_bye(self, event, gid, toks: list) -> Tuple[bool, str]:
        """退群提示：bye on/off/set <消息>"""
        if not toks:
            return False, "用法：bye on/off | bye set <消息文本>"
        sub = toks[0].lower()
        bye_conf = self.state.setdefault("bye", {}).setdefault(str(gid), {"enabled": False, "msg": "{user} 离开了本群"})
        
        if sub == "on":
            bye_conf["enabled"] = True
            self._save_state()
            return True, "已开启退群提示"
        elif sub == "off":
            bye_conf["enabled"] = False
            self._save_state()
            return True, "已关闭退群提示"
        elif sub == "set":
            msg = " ".join(toks[1:]).strip()
            if not msg:
                return False, "用法：bye set <消息文本>（可用 {user} 占位符）"
            bye_conf["msg"] = msg
            self._save_state()
            return True, f"已设置退群消息：{msg}"
        return False, "未知子命令"

    async def _do_bc(self, event, gid, toks: list) -> Tuple[bool, str]:
        """群公告：bc <公告内容>"""
        if not toks:
            return False, "用法：bc <公告内容>"
        content = " ".join(toks).strip()
        try:
            ob = self._ob(event)
            await ob.call("_send_group_notice", group_id=int(gid), content=content)
            return True, f"已发布群公告"
        except Exception as e:
            return False, f"发布公告失败: {e}"

    async def _do_g(self, event, gid, toks: list) -> Tuple[bool, str]:
        """群操作：g nn <新群名>"""
        if not toks:
            return False, "用法：g nn <新群名>"
        sub = toks[0].lower()
        if sub == "nn":
            new_name = " ".join(toks[1:]).strip()
            if not new_name:
                return False, "用法：g nn <新群名>"
            try:
                ob = self._ob(event)
                await ob.call("set_group_name", group_id=int(gid), group_name=new_name)
                return True, f"已将群名改为：{new_name}"
            except Exception as e:
                return False, f"修改群名失败: {e}"
        return False, "未知子命令（可用：nn=改名）"

    async def _dispatch(self, event: AstrMessageEvent, op: str, gid, toks: list) -> Tuple[bool, str]:
        if op == "timeout":
            try:
                default_dur = int(self.config.get("default_mute_seconds", 600) or 600)
                max_dur = int(self.config.get("max_mute_seconds", 2592000) or 2592000)
            except Exception:
                default_dur, max_dur = 600, 2592000
            # 显式给了 0（或"0s/0秒"）= 解除禁言；未给数字才用默认时长。
            # 注意 first_int 返回 None 表示未给数字，不能与 0 混淆（0 是合法撤销指令）。
            if any(t.isdigit() and int(t) == 0 for t in toks):
                dur = 0
            else:
                dur = first_int(toks) or default_dur
                dur = max(1, min(dur, max_dur))
            target = extract_target_qq(event, toks, numeric_param_first=True)
            if not target:
                if chain_has_at_self(event):
                    return False, "不能对机器人自己禁言。若要禁言他人：timeout <秒数> <@用户/QQ号>"
                return False, "用法：timeout <秒数> <@用户/QQ号> [理由]；timeout 0 <@用户> = 解除其禁言"
            reason = extract_reason(toks, target)
            return await self._do_timeout(event, gid, target, dur, reason=reason)
        if op == "kick":
            target = extract_target_qq(event, toks)
            if not target:
                if chain_has_at_self(event):
                    return False, "不能对机器人自己执行踢出。若要踢他人：kick <@用户/QQ号>"
                return False, "用法：kick <@用户/QQ号> [理由]"
            reason = extract_reason(toks, target)
            return await self._do_kick(event, gid, target, blacklist=False, reason=reason)
        if op == "ban":
            target = extract_target_qq(event, toks, numeric_param_first=False)
            if not target:
                return False, "用法：ban <@用户/QQ号> [理由]"
            reason = extract_reason(toks, target)
            return await self._do_kick(event, gid, target, blacklist=True, reason=reason)
        if op == "unban":
            target = extract_target_qq(event, toks, numeric_param_first=False)
            if not target:
                return False, "用法：unban <@用户/QQ号>"
            return await self._do_unban(event, gid, target)
        if op == "warn":
            if toks and toks[0].lower() == "max":
                n = first_int(toks[1:])
                if n is None or n < 1:
                    return False, "用法：warn max <次数>"
                self.state["warn_max"] = n
                self._save_state()
                return True, f"全局警告次数上限已设为 {n}"
            # warn clear <@用户/QQ号>：清除该用户警告计数（撤销误警告）
            if toks and toks[0].lower() == "clear":
                target = extract_target_qq(event, toks[1:], numeric_param_first=False)
                if not target:
                    return False, "用法：warn clear <@用户/QQ号>"
                warns = self.state.setdefault("warns", {}).setdefault(str(gid), {})
                if str(target) not in warns:
                    return False, f"{target} 没有警告记录"
                old = warns.pop(str(target))
                self._save_state()
                return True, f"已清除 {target} 的警告记录（原 {old} 次）"
            target = extract_target_qq(event, toks, numeric_param_first=False)
            if not target:
                return False, "用法：warn <@用户/QQ号> [理由] 或 warn max <次数> 或 warn clear <@用户/QQ号>"
            reason = extract_reason(toks, target)
            return await self._do_warn(event, gid, target, reason=reason)
        if op == "recall":
            return await self._do_recall(event, gid, toks)
        if op == "mute":
            return await self._do_mute(event, gid)
        if op == "status":
            target = extract_target_qq(event, toks, numeric_param_first=False)
            if target == str(event.get_self_id()):
                target = ""  # @bot = 查全群总览
            return await self._do_status(event, gid, target)
        if op == "admin":
            return await self._do_admin(event, gid, toks)
        if op == "unmute":
            target = extract_target_qq(event, toks, numeric_param_first=False)
            if not target:
                if chain_has_at_self(event):
                    return False, "不能对机器人自己解禁。若要解禁他人：unmute <@用户/QQ号>"
                return False, "用法：unmute <@用户/QQ号>"
            return await self._do_unmute(event, gid, target)
        if op == "mutelist":
            return await self._do_mutelist(event, gid)
        if op == "muteall":
            try:
                default_dur = int(self.config.get("default_mute_seconds", 600) or 600)
                max_dur = int(self.config.get("max_mute_seconds", 2592000) or 2592000)
            except Exception:
                default_dur, max_dur = 600, 2592000
            if any(t.isdigit() and int(t) == 0 for t in toks):
                dur = 0
            else:
                dur = first_int(toks) or default_dur
                dur = max(0, min(dur, max_dur))
            return await self._do_muteall(event, gid, dur)
        if op == "banlist":
            return await self._do_banlist(event, gid)
        if op == "title":
            return await self._do_title(event, gid, toks)
        if op == "sw":
            return await self._do_sw(event, gid, toks)
        if op == "antiflood":
            return await self._do_antiflood(event, gid, toks)
        if op == "cardcheck":
            return await self._do_cardcheck(event, gid, toks)
        if op == "ad":
            return await self._do_ad(event, gid, toks)
        if op == "adban":
            return await self._do_adban(event, gid, toks)
        if op == "wel":
            return await self._do_wel(event, gid, toks)
        if op == "bye":
            return await self._do_bye(event, gid, toks)
        if op == "bc":
            return await self._do_bc(event, gid, toks)
        if op == "g":
            return await self._do_g(event, gid, toks)
        return False, f"未知操作: {op}"

    # ---------------- 命令入口 ----------------
    async def _run_command(self, event: AstrMessageEvent, op: str):
        gate = self._gated(event)
        if gate:
            yield event.plain_result(f"⛔ [群管] {gate}")
            return
        if not await self._authorized(event):
            yield event.plain_result("⛔ [群管] 权限不足：仅本群群主/管理员可使用。")
            return
        _, toks = parse_command_tokens(event.message_str)
        if op == "status" and toks and toks[0].lower() == "all":
            yield event.plain_result("❌ [群管] status 仅查询单个群，不支持 all。")
            return
        # 全局批量化：<命令> all ...（作用于机器人管理的所有群/为其群主的所有群）
        if toks and toks[0].lower() == "all" and op != "recall" or (op == "recall" and toks and toks[0].lower() == "all"):
            gtoks = toks[1:]
            if op == "recall" and getattr(event.message_obj, "reply", None) is not None:
                # 引用撤回不参与全局，按单群处理
                gtoks = None
            if gtoks is not None:
                try:
                    groups = await self._global_groups(event)
                except Exception as e:
                    yield event.plain_result(f"❌ [群管] 获取群列表失败: {e}")
                    return
                if not groups:
                    yield event.plain_result("❌ [群管] 没有机器人担任管理员/群主的群。")
                    return
                lines, okc = [], 0
                for gid, _role in groups:
                    try:
                        ok, msg = await self._dispatch(event, op, gid, gtoks)
                    except Exception as e:
                        ok, msg = False, f"异常: {e}"
                    okc += 1 if ok else 0
                    lines.append(("✅" if ok else "❌") + f" {gid}: {msg}")
                header = f"【全局·{OP_NAMES.get(op, op)}】成功 {okc}/{len(groups)}"
                yield event.plain_result(f"[群管] {header}\n" + "\n".join(lines[:40]))
                return
        gid = str(event.get_group_id())
        try:
            ok, msg = await self._dispatch(event, op, gid, toks)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        yield event.plain_result(f"[群管] {'✅ ' if ok else '❌ '}{msg}")

    @filter.command("timeout")
    async def cmd_timeout(self, event: AstrMessageEvent):
        """timeout [all] <秒数> <@用户/QQ号>：禁言该用户 N 秒。"""
        async for r in self._run_command(event, "timeout"):
            yield r

    @filter.command("kick")
    async def cmd_kick(self, event: AstrMessageEvent):
        """kick [all] <@用户/QQ号>：将该用户移出群聊。"""
        async for r in self._run_command(event, "kick"):
            yield r

    @filter.command("ban")
    async def cmd_ban(self, event: AstrMessageEvent):
        """ban [all] <@用户/QQ号>：移出群聊并拉黑，拒绝其再次入群。"""
        async for r in self._run_command(event, "ban"):
            yield r

    @filter.command("unban")
    async def cmd_unban(self, event: AstrMessageEvent):
        """unban [all] <@用户/QQ号>：解除拉黑。"""
        async for r in self._run_command(event, "unban"):
            yield r

    @filter.command("warn")
    async def cmd_warn(self, event: AstrMessageEvent):
        """warn [all] <@用户/QQ号>；warn max <次数>：设定警告上限。"""
        async for r in self._run_command(event, "warn"):
            yield r

    @filter.command("recall")
    async def cmd_recall(self, event: AstrMessageEvent):
        """引用消息 + recall 撤回该条；recall [all] <条数> <@用户/QQ号> 撤回其最近 N 条。"""
        async for r in self._run_command(event, "recall"):
            yield r

    @filter.command("mute")
    async def cmd_mute(self, event: AstrMessageEvent):
        """mute [all]：单发一次开关全员禁言。"""
        async for r in self._run_command(event, "mute"):
            yield r

    @filter.command("admin")
    async def cmd_admin(self, event: AstrMessageEvent):
        """admin set/remove <@用户/QQ号>：设置/取消群管理员（需机器人为群主）。"""
        async for r in self._run_command(event, "admin"):
            yield r

    @filter.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """status [@用户/QQ号]：查询本群禁言/警告/拉黑状态与理由，无参=全群总览（仅本群群主/管理员）。"""
        async for r in self._run_command(event, "status"):
            yield r

    @filter.command("unmute")
    async def cmd_unmute(self, event: AstrMessageEvent):
        """unmute <@用户/QQ号>：解除该用户的禁言。"""
        async for r in self._run_command(event, "unmute"):
            yield r

    @filter.command("mutelist")
    async def cmd_mutelist(self, event: AstrMessageEvent):
        """mutelist：列出当前群所有被禁言的成员。"""
        async for r in self._run_command(event, "mutelist"):
            yield r

    @filter.command("muteall")
    async def cmd_muteall(self, event: AstrMessageEvent):
        """muteall [时长秒数|0]：开启全员禁言（0=关闭）。"""
        async for r in self._run_command(event, "muteall"):
            yield r

    @filter.command("banlist")
    async def cmd_banlist(self, event: AstrMessageEvent):
        """banlist：列出当前群拉黑名单。"""
        async for r in self._run_command(event, "banlist"):
            yield r

    @filter.command("title")
    async def cmd_title(self, event: AstrMessageEvent):
        """title <@用户> <头衔文本>：设置群成员头衔。"""
        async for r in self._run_command(event, "title"):
            yield r

    @filter.command("sw")
    async def cmd_sw(self, event: AstrMessageEvent):
        """sw add/del/list/on/off/set：敏感词管理。"""
        async for r in self._run_command(event, "sw"):
            yield r

    @filter.command("antiflood")
    async def cmd_antiflood(self, event: AstrMessageEvent):
        """antiflood on/off/set：刷屏检测。"""
        async for r in self._run_command(event, "antiflood"):
            yield r

    @filter.command("cardcheck")
    async def cmd_cardcheck(self, event: AstrMessageEvent):
        """cardcheck on/off：名片检测。"""
        async for r in self._run_command(event, "cardcheck"):
            yield r

    @filter.command("ad")
    async def cmd_ad(self, event: AstrMessageEvent):
        """ad on/off/set：广告检测。"""
        async for r in self._run_command(event, "ad"):
            yield r

    @filter.command("adban")
    async def cmd_adban(self, event: AstrMessageEvent):
        """adban mute/kick/ban：广告拦截行为。"""
        async for r in self._run_command(event, "adban"):
            yield r

    @filter.command("wel")
    async def cmd_wel(self, event: AstrMessageEvent):
        """wel on/off/set：入群欢迎。"""
        async for r in self._run_command(event, "wel"):
            yield r

    @filter.command("bye")
    async def cmd_bye(self, event: AstrMessageEvent):
        """bye on/off/set：退群提示。"""
        async for r in self._run_command(event, "bye"):
            yield r

    @filter.command("bc")
    async def cmd_bc(self, event: AstrMessageEvent):
        """bc <公告内容>：发布群公告。"""
        async for r in self._run_command(event, "bc"):
            yield r

    @filter.command("g")
    async def cmd_g(self, event: AstrMessageEvent):
        """g nn <新群名>：群操作。"""
        async for r in self._run_command(event, "g"):
            yield r


    # ---------------- LLM 工具（@Bot 自然语言兜底） ----------------
    def _llm_group_gate(self, event: AstrMessageEvent) -> Optional[str]:
        if event.is_private_chat() or not self._is_group(event):
            return None
        return str(event.get_group_id())

    async def _llm_perm_gate(self, event: AstrMessageEvent) -> Optional[str]:
        if not await self._authorized(event):
            return "权限不足：仅本群群主/管理员可执行该操作。"
        return None

    @llm_tool(name="gm_timeout_user")
    async def tool_timeout(self, event: AstrMessageEvent, user_id: str, duration_sec: int = 600, reason: str = ""):
        """禁言当前群内指定用户。user_id 为目标 QQ 号，duration_sec 为禁言秒数（0~2592000，0 表示解除禁言），reason 为可选理由（记入 status 展示）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_timeout(event, gid, str(user_id), max(0, min(int(duration_sec), 2592000)), reason=str(reason or "")[:100])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_kick_user")
    async def tool_kick(self, event: AstrMessageEvent, user_id: str, reason: str = ""):
        """将当前群内指定用户移出群聊（不拉黑）。user_id 为目标 QQ 号，reason 为可选理由（记入状态档案，status 可查）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_kick(event, gid, str(user_id), blacklist=False, reason=str(reason or "")[:100])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_ban_user")
    async def tool_ban(self, event: AstrMessageEvent, user_id: str, reason: str = ""):
        """将当前群内指定用户移出群聊并拉黑（自动拒绝其后续入群申请）。user_id 为目标 QQ 号，reason 为可选理由（记入状态档案，status 可查）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_kick(event, gid, str(user_id), blacklist=True, reason=str(reason or "")[:100])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_unban_user")
    async def tool_unban(self, event: AstrMessageEvent, user_id: str):
        """解除当前群内指定用户的拉黑（允许其重新申请入群）。user_id 为目标 QQ 号。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_unban(event, gid, str(user_id))
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_warn_user")
    async def tool_warn(self, event: AstrMessageEvent, user_id: str, reason: str = ""):
        """给当前群内指定用户记一次警告；达到上限自动移出群聊。user_id 为目标 QQ 号，reason 为可选理由（记入状态档案，status 可查）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_warn(event, gid, str(user_id), reason=str(reason or "")[:100])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_recall_user_messages")
    async def tool_recall(self, event: AstrMessageEvent, user_id: str = "", count: int = 1):
        """撤回当前群内的消息。引用某条消息并让模型调用本工具=撤回该条引用消息；否则撤回 user_id（目标 QQ 号）最近的 count 条消息（1~50）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            toks = [str(max(1, min(int(count), 50))), str(user_id)] if str(user_id or "").strip() else []
            ok, msg = await self._do_recall(event, gid, toks)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_toggle_whole_mute")
    async def tool_mute(self, event: AstrMessageEvent):
        """切换（开启/关闭）当前群的全员禁言。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_mute(event, gid)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_set_admin")
    async def tool_admin(self, event: AstrMessageEvent, user_id: str, grant: bool = True):
        """设置/取消当前群内指定用户的群管理员（需机器人为群主）。user_id 为目标 QQ 号；grant=True 设为管理员，False 取消。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_admin(event, gid, ["set" if grant else "remove", str(user_id)])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_status")
    async def tool_status(self, event: AstrMessageEvent, user_id: str = ""):
        """查询当前群的管理状态：无 user_id=全群总览（谁被禁言/禁言剩余、警告计数、拉黑名单与理由）；带 user_id（QQ 号）=该用户在本群的档案。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        target = str(user_id).strip() if user_id else ""
        if target == str(event.get_self_id()):
            target = ""  # 查 bot 自己 = 总览
        try:
            ok, msg = await self._do_status(event, gid, target)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return msg

    @llm_tool(name="gm_set_warn_max")
    async def tool_warn_max(self, event: AstrMessageEvent, max_count: int):
        """设定当前群管系统的全局警告次数上限（用户被警告达到该次数自动移出群聊）。max_count 为正整数。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        n = int(max_count)
        if n < 1:
            return "❌ 警告上限必须 ≥ 1"
        try:
            ok, msg = await self._dispatch(event, "warn", gid, ["max", str(n)])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_clear_warn")
    async def tool_warn_clear(self, event: AstrMessageEvent, user_id: str):
        """清除当前群内指定用户的警告计数（撤销误警告）。user_id 为目标 QQ 号。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._dispatch(event, "warn", gid, ["clear", str(user_id)])
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    # ---------------- LLM 自然语言兜底：信息查询 / 互动工具（v1.1.0） ----------------
    # 这 9 个工具与上方 11 个管理类工具共用 _llm_group_gate / _llm_perm_gate 权限门；
    # 信息类工具（查询群/成员/历史）权限与群主/管理员一致。
    async def _data_res(self, res):
        """从 _ob 返回值提取 data 字段（call_action 有时直接返 data，有时套 data）。"""
        if isinstance(res, dict) and isinstance(res.get("data"), (dict, list)):
            return res["data"]
        return res

    @llm_tool(name="gm_get_group_info")
    async def tool_get_group_info(self, event: AstrMessageEvent):
        """查询当前群的基本信息（群号、群名称、群成员数等）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            res = await self._data_res(await self._ob(event, "get_group_info", group_id=int(gid), no_cache=True))
            if not isinstance(res, dict):
                return "❌ 群信息不可查"
            name = res.get("group_name", "") or res.get("name", "")
            members = res.get("member_count", "") or res.get("total_member_count", "")
            owner = res.get("owner_id", "") or res.get("group_owner", "")
            max_m = res.get("max_member_count", "")
            lines = [f"📌 群 {gid}｜{name or '（无名）'}"]
            if members != "":
                lines.append(f"   👥 成员数：{members}" + (f"/{max_m}" if max_m else ""))
            if owner:
                lines.append(f"   👑 群主：{owner}")
            # 拼接一些常见字段
            extras = []
            for k in ("group_level", "create_time"):
                if res.get(k) not in (None, ""):
                    extras.append(f"{k}={res.get(k)}")
            if extras:
                lines.append("   ℹ️ " + " ｜ ".join(extras))
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 群信息查询失败：{e}"

    @llm_tool(name="gm_get_member_info")
    async def tool_get_member_info(self, event: AstrMessageEvent, user_id: str):
        """查询当前群内指定成员的详细信息（群名片、角色、加群时间、最后发言等）。user_id 为目标 QQ 号。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return "❌ user_id 必须是纯数字 QQ 号"
        try:
            res = await self._data_res(await self._ob(
                event, "get_group_member_info",
                group_id=int(gid), user_id=int(uid), no_cache=True,
            ))
            if not isinstance(res, dict):
                return f"❌ 未找到成员 {uid}（可能已退群）"
            role = res.get("role", "") or "未知"
            role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
            card = res.get("card", "") or res.get("nickname", "") or ""
            nick = res.get("nickname", "")
            title = res.get("title", "")  # 专属头衔
            join_ts = res.get("join_time", 0) or 0
            last_ts = res.get("last_sent_time", 0) or 0
            shut = res.get("shut_up_timestamp", 0) or 0
            lines = [
                f"👤 {uid}｜{card or '（无群名片）'}",
                f"   🆔 昵称：{nick}" if nick and nick != card else None,
                f"   🛡️ 群身份：{role_cn}",
                f"   🎖️ 专属头衔：{title}" if title else None,
                f"   📅 加群时间：{self._fmt_ts(join_ts)}" if join_ts else None,
                f"   💬 最后发言：{self._fmt_ts(last_ts)}" if last_ts else None,
            ]
            if shut and int(shut) > int(time.time()):
                lines.append(f"   🔇 禁言至：{self._fmt_ts(shut)}")
            return "\n".join([x for x in lines if x])
        except Exception as e:
            return f"❌ 成员信息查询失败：{e}"

    @llm_tool(name="gm_get_member_list")
    async def tool_get_member_list(self, event: AstrMessageEvent, limit: int = 30):
        """获取当前群的成员列表（默认 30 个，最多 200）。返回 QQ 号 + 群名片 + 角色。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            n = max(1, min(int(limit), 200))
            res = await self._data_res(await self._ob(event, "get_group_member_list", group_id=int(gid), no_cache=True))
            members = res if isinstance(res, list) else []
            role_cn = {"owner": "👑", "admin": "🛡️", "member": "  "}
            lines = [f"👥 群 {gid} 成员（前 {min(n, len(members))}/{len(members)}）："]
            for m in members[:n]:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("user_id", "") or "")
                if not uid:
                    continue
                card = m.get("card", "") or m.get("nickname", "") or ""
                role = m.get("role", "member")
                lines.append(f"  {role_cn.get(role, '  ')} {uid}｜{card}")
            if len(lines) == 1:
                return "❌ 成员列表为空（可能无权限或群空）"
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 成员列表查询失败：{e}"

    @llm_tool(name="gm_get_msg_history")
    async def tool_get_msg_history(self, event: AstrMessageEvent, count: int = 20, user_id: str = ""):
        """读取当前群最近的消息历史（默认 20 条，最多 100）。可选 user_id 只看某用户的消息。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            n = max(1, min(int(count), 100))
            target_uid = str(user_id or "").strip()
            res = await self._data_res(await self._ob(
                event, "get_group_msg_history",
                group_id=int(gid), message_seq=None, count=n,
            ))
            # 兼容 get_group_msg_history 的多种返回形态
            messages = []
            if isinstance(res, dict):
                messages = res.get("messages", []) or []
            elif isinstance(res, list):
                messages = res
            if not messages:
                return "❌ 历史消息为空（可能无权限或 NapCat 未启用）"
            if target_uid:
                messages = [m for m in messages if str((m or {}).get("user_id", "")) == target_uid]
            if not messages:
                return f"❌ 该用户 {target_uid} 在最近 {n} 条消息内没有发言"
            lines = [f"📜 群 {gid} 最近 {min(n, len(messages))} 条消息" + (f"（只看 {target_uid}）" if target_uid else "")]
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = m.get("message_id", "")
                sender = m.get("sender", {}) if isinstance(m.get("sender"), dict) else {}
                uid = str(sender.get("user_id", m.get("user_id", "")) or "")
                nick = sender.get("nickname", "") or ""
                ts = int(m.get("time", 0) or 0)
                # 提取纯文本（忽略 image/face/at 等组件）
                raw = m.get("message", "")
                txt = ""
                if isinstance(raw, list):
                    parts = []
                    for seg in raw:
                        if isinstance(seg, dict):
                            t = seg.get("type", "")
                            if t == "text":
                                parts.append(seg.get("data", {}).get("text", ""))
                            elif t == "at":
                                parts.append("@" + str(seg.get("data", {}).get("qq", "")))
                    txt = "".join(parts)
                else:
                    txt = str(raw or "")
                txt = (txt or "").replace("\n", " ").strip()[:120]
                lines.append(f"  [{self._fmt_ts(ts)}] {uid}({nick})：{txt or '（非文本消息）'}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 历史消息查询失败：{e}"

    @llm_tool(name="gm_at_member")
    async def tool_at_member(self, event: AstrMessageEvent, user_id: str, text: str = ""):
        """在当前群中以 At 形式发一条消息 @ 指定成员。user_id 为目标 QQ 号，text 为附带文本（可空）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return "❌ user_id 必须是纯数字 QQ 号"
        try:
            chain = [At(qq=int(uid)), Plain(" " + (str(text or "").strip()))]
            from astrbot.core.message.message_event_result import MessageChain
            await event.send(MessageChain(chain=chain))
            return "✅ 已发送 @ 消息"
        except Exception as e:
            return f"❌ 发送失败：{e}"

    @llm_tool(name="gm_at_all")
    async def tool_at_all(self, event: AstrMessageEvent, text: str = ""):
        """在当前群中 @全体成员（需机器人具备管理员或群主身份）。text 为附带文本（可空）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        bot_ok, bot_msg = await self._bot_gate(event, gid, need_owner=False)
        if not bot_ok:
            return f"❌ {bot_msg}"
        try:
            chain = [At(qq="all"), Plain(" " + (str(text or "").strip()))]
            from astrbot.core.message.message_event_result import MessageChain
            await event.send(MessageChain(chain=chain))
            return "✅ 已 @全体成员"
        except Exception as e:
            return f"❌ 发送失败：{e}"

    @llm_tool(name="gm_set_group_card")
    async def tool_set_card(self, event: AstrMessageEvent, user_id: str, card: str = ""):
        """修改当前群内指定成员的群名片。user_id 为目标 QQ 号，card 为新群名片（空字符串=清空）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        bot_ok, bot_msg = await self._bot_gate(event, gid, need_owner=False)
        if not bot_ok:
            return f"❌ {bot_msg}"
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return "❌ user_id 必须是纯数字 QQ 号"
        try:
            await self._ob(event, "set_group_card", group_id=int(gid), user_id=int(uid), card=str(card or "")[:60])
            return f"✅ 已将 {uid} 的群名片改为：{card or '（已清空）'}"
        except Exception as e:
            return f"❌ 修改群名片失败：{e}"

    @llm_tool(name="gm_set_group_essence")
    async def tool_set_essence(self, event: AstrMessageEvent, message_id: str):
        """将指定消息设为群精华（再次调用同一 message_id 取消精华）。message_id 为消息 ID（可从 get_msg_history 获取）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        bot_ok, bot_msg = await self._bot_gate(event, gid, need_owner=False)
        if not bot_ok:
            return f"❌ {bot_msg}"
        mid = str(message_id or "").strip()
        if not mid.isdigit():
            return "❌ message_id 必须是纯数字"
        try:
            # OneBot v11 没有 set_essence 的标准动作；用 NapCat 扩展的 set_essence_message
            try:
                await self._ob(event, "set_essence_message", message_id=int(mid))
                return f"✅ 已将消息 {mid} 设为群精华"
            except Exception:
                # 兜底：有些适配器叫 delete_essence_message 取消
                await self._ob(event, "delete_essence_message", message_id=int(mid))
                return f"✅ 已取消消息 {mid} 的精华"
        except Exception as e:
            return f"❌ 设置精华失败：{e}"

    @llm_tool(name="gm_send_group_notice")
    async def tool_send_notice(self, event: AstrMessageEvent, content: str):
        """在当前群发布一条群公告。content 为公告内容（建议 50 字以内）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        bot_ok, bot_msg = await self._bot_gate(event, gid, need_owner=False)
        if not bot_ok:
            return f"❌ {bot_msg}"
        text = (content or "").strip()
        if not text:
            return "❌ 公告内容不能为空"
        if len(text) > 200:
            text = text[:200]
        try:
            # NapCat / go-cqhttp 的动作名：_send_group_notice；OneBot v11 标准为 send_group_notice
            try:
                await self._ob(event, "_send_group_notice", group_id=int(gid), content=text)
            except Exception:
                await self._ob(event, "send_group_notice", group_id=int(gid), content=text)
            return f"✅ 已发布群公告：{text[:30]}{'…' if len(text) > 30 else ''}"
        except Exception as e:
            return f"❌ 群公告发布失败：{e}"

    # ---------------- LLM 请求阶段：注入 AT 标签提示词 ----------------
    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 实际请求前，给 system_prompt 追加 AT 标签用法提示，让大模型在需要时输出 [at:QQ] / [at:all]。
        通过配置开关 enable_at_feature 控制（默认开）。
        """
        if not self.config.get("enable_at_feature", True):
            return
        # 仅在群聊中注入（私聊也允许 @ Bot 自己，但 [at:all] 无意义；保险起见仅限群）
        try:
            if not event.get_group_id():
                return
        except Exception:
            return
        req.system_prompt = (req.system_prompt or "") + AT_INSTRUCTION

    # ---------------- LLM 装饰阶段：把 [at:QQ] / [at:all] 转原生 At 组件 ----------------
    @filter.on_decorating_result(priority=10)
    async def process_at_tags(self, event: AstrMessageEvent):
        """把 LLM 输出 Plain 文本中的 [at:QQ] / [at:all] 标签解析为原生 At 组件（参考 QQ群大模型管理工具）。
        关闭后 = 不做替换，标签原样发出。
        """
        if not self.config.get("enable_at_feature", True):
            return
        try:
            result = event.get_result()
        except Exception:
            return
        if not result or not getattr(result, "chain", None):
            return
        new_chain: List[BaseMessageComponent] = []
        hit = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in (comp.text or ""):
                hit = True
                text = comp.text
                last = 0
                for m in AT_PATTERN.finditer(text):
                    s, e = m.span()
                    if s > last:
                        new_chain.append(Plain(text[last:s]))
                    uid = m.group("uid")
                    if uid.isdigit():
                        new_chain.append(At(qq=uid))
                    else:
                        new_chain.append(At(qq="all"))
                    new_chain.append(Plain(" "))
                    last = e
                if last < len(text):
                    new_chain.append(Plain(text[last:]))
            else:
                new_chain.append(comp)
        if hit:
            result.chain = new_chain

    # ---------------- 拉黑用户入群申请自动拒绝 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_request(self, event: AstrMessageEvent):
        """request/group/add 事件：被拉黑用户的入群申请自动拒绝。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request" or raw.get("request_type") != "group":
            return
        if raw.get("sub_type") != "add":
            return
        gid = str(raw.get("group_id", "") or "")
        uid = str(raw.get("user_id", "") or "")
        if not gid or not uid:
            return
        bans = self.state.get("bans", {}).get(gid, {})
        if uid not in bans:
            return
        flag = raw.get("flag", "")
        reason = str(self.config.get("reject_reason", "你已被本群拉黑，如有疑问请联系群管理"))
        try:
            await self._ob(
                event, "set_group_add_request",
                flag=flag, sub_type="add", approve=False, reason=reason,
            )
            logger.info(f"{LOG} 已自动拒绝被拉黑用户 {uid} 的入群申请（群 {gid}）")
        except Exception as e:
            logger.error(f"{LOG} 拒绝入群申请失败（群 {gid} 用户 {uid}）: {e}")
        event.stop_event()

    @llm_tool(name="gm_unmute_user")
    async def tool_unmute(self, event: AstrMessageEvent, user_id: str):
        """解除当前群内指定用户的禁言。user_id 为目标 QQ 号。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_unmute(event, gid, str(user_id))
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_muteall_toggle")
    async def tool_muteall(self, event: AstrMessageEvent, duration_sec: int = 0):
        """开启/关闭当前群全员禁言。duration_sec=0 表示关闭，>0 表示开启（注意：QQ 全员禁言需手动关闭）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_muteall(event, gid, int(duration_sec))
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_set_title")
    async def tool_set_title(self, event: AstrMessageEvent, user_id: str, title: str):
        """设置当前群内指定用户的头衔。user_id 为目标 QQ 号，title 为头衔文本。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            # 构造假的 toks 以复用 _do_title
            toks = [str(user_id), str(title)]
            ok, msg = await self._do_title(event, gid, toks)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_send_group_notice")
    async def tool_send_notice(self, event: AstrMessageEvent, content: str):
        """发布当前群公告。content 为公告内容。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            toks = [str(content)]
            ok, msg = await self._do_bc(event, gid, toks)
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg



    async def initialize(self):
        """插件加载后启动后台任务。"""
        import asyncio
        # 启动超长禁言续期任务
        task = asyncio.create_task(self._mute_renewal_ticker())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(f"{LOG} 插件已加载 v1.1.0，后台任务已启动")

    async def _mute_renewal_ticker(self):
        """后台任务：每分钟检查超长禁言续期。"""
        import asyncio
        while True:
            try:
                await asyncio.sleep(60)
                await self._renew_long_mutes()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"{LOG} 禁言续期任务异常: {e}")

    async def _renew_long_mutes(self):
        """扫描超长禁言记录，到期前续期下一段。"""
        import time
        now = int(time.time())
        memo = self.state.get("mute_memo", {})
        renewed_count = 0
        for gid, users in list(memo.items()):
            for uid, exp in list(users.items()):
                if exp <= now:
                    continue
                remaining = exp - now
                if remaining <= MUTE_CHUNK:
                    continue
                # 检查是否到了续期时间
                renew_key = f"renew_{gid}_{uid}"
                last_renew = self.state.get("last_renew", {}).get(renew_key, 0)
                if now - last_renew < MUTE_CHUNK - RENEW_LEAD:
                    continue
                # 执行续期（需要通过 platform_manager 获取 client）
                try:
                    client = None
                    for platform in self.context.platform_manager.platform_insts:
                        if platform.meta().name == "aiocqhttp":
                            client = platform.get_client()
                            break
                    if not client:
                        continue
                    chunk = min(remaining, MUTE_CHUNK)
                    await client.api.call_action("set_group_ban", group_id=int(gid), user_id=int(uid), duration=chunk)
                    self.state.setdefault("last_renew", {})[renew_key] = now
                    self._save_state()
                    renewed_count += 1
                    logger.info(f"{LOG} 已为 {uid}@{gid} 续期禁言，剩余 {remaining//3600}小时")
                except Exception as e:
                    logger.warning(f"{LOG} 续期禁言失败（群{gid} 用户{uid}）: {e}")
        if renewed_count > 0:
            logger.info(f"{LOG} 本轮续期了 {renewed_count} 个超长禁言")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=3)
    async def on_group_notice_events(self, event: AstrMessageEvent):
        """群成员增减事件统一入口"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        
        notice_type = raw.get("notice_type")
        if notice_type == "group_increase":
            await self._on_member_join(event, raw)
        elif notice_type == "group_decrease":
            await self._on_member_leave(event, raw)
    
    async def _on_member_join(self, event: AstrMessageEvent, raw: dict):
        """成员入群：欢迎消息 + 名片检测"""
        gid = str(raw.get("group_id") or "")
        if not gid:
            return
        
        # 欢迎消息
        wel_conf = self.state.get("wel", {}).get(gid, {})
        if wel_conf.get("enabled"):
            try:
                new_uid = str(raw.get("user_id") or "")
                if not new_uid:
                    return
                msg = wel_conf.get("msg", "欢迎 {user} 加入本群！")
                msg = msg.replace("{user}", f"[CQ:at,qq={new_uid}]")
                ob = self._ob(event)
                await ob.call("send_group_msg", group_id=int(gid), message=msg)
            except Exception as e:
                logger.warning(f"{LOG} 发送欢迎消息失败: {e}")
        
        # 名片检测（简化版：检查昵称是否含特定关键词，实际可扩展）
        card_conf = self.state.get("cardcheck", {}).get(gid, {})
        if card_conf.get("enabled"):
            # 占位实现：真实场景需获取成员信息并检查名片
            pass

    async def _on_member_leave(self, event: AstrMessageEvent, raw: dict):
        """成员退群：退群提示"""
        gid = str(raw.get("group_id") or "")
        if not gid:
            return
        
        bye_conf = self.state.get("bye", {}).get(gid, {})
        if bye_conf.get("enabled"):
            try:
                left_uid = str(raw.get("user_id") or "")
                if not left_uid:
                    return
                msg = bye_conf.get("msg", "{user} 离开了本群")
                msg = msg.replace("{user}", left_uid)
                ob = self._ob(event)
                await ob.call("send_group_msg", group_id=int(gid), message=msg)
            except Exception as e:
                logger.warning(f"{LOG} 发送退群提示失败: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=5)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息：敏感词/刷屏/广告检测"""
        if not self._is_group(event):
            return
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        msg_text = event.message_str or ""
        
        # 敏感词检测
        sw_conf = self.state.get("sw", {}).get(gid, {})
        if sw_conf.get("enabled"):
            words = sw_conf.get("words", [])
            for word in words:
                if word in msg_text:
                    try:
                        dur = sw_conf.get("duration", DEFAULT_SW_DURATION)
                        ob = self._ob(event)
                        await ob.call("delete_msg", message_id=event.message_obj.message_id)
                        await ob.call("set_group_ban", group_id=int(gid), user_id=int(uid), duration=dur)
                        logger.info(f"{LOG} 敏感词触发：群{gid} 用户{uid} 触发[{word}]，禁言{dur}秒")
                    except Exception as e:
                        logger.warning(f"{LOG} 敏感词处理失败: {e}")
                    return
        
        # 刷屏检测（简化版：滑动窗口计数）
        flood_conf = self.state.get("flood", {}).get(gid, {})
        if flood_conf.get("enabled"):
            import time
            now = int(time.time())
            threshold = flood_conf.get("threshold", DEFAULT_FLOOD_THRESHOLD)
            window = flood_conf.get("window", DEFAULT_FLOOD_WINDOW)
            
            flood_state = self.state.setdefault("flood_state", {}).setdefault(gid, {})
            user_msgs = flood_state.setdefault(uid, [])
            user_msgs.append(now)
            # 清理过期记录
            user_msgs[:] = [t for t in user_msgs if now - t <= window]
            
            if len(user_msgs) >= threshold:
                try:
                    ob = self._ob(event)
                    await ob.call("set_group_ban", group_id=int(gid), user_id=int(uid), duration=600)
                    logger.info(f"{LOG} 刷屏触发：群{gid} 用户{uid} {window}秒内{len(user_msgs)}条消息")
                    user_msgs.clear()
                except Exception as e:
                    logger.warning(f"{LOG} 刷屏处理失败: {e}")
                return
        
        # 广告检测（简化版：评分制）
        ad_conf = self.state.get("ad", {}).get(gid, {})
        if ad_conf.get("enabled"):
            score = 0
            # 简单启发式：包含链接+联系方式
            if "http://" in msg_text or "https://" in msg_text:
                score += 5
            if any(k in msg_text for k in ["加群", "扫码", "微信", "QQ群"]):
                score += 3
            if any(k in msg_text for k in ["优惠", "代购", "刷单", "兼职"]):
                score += 4
            
            threshold = ad_conf.get("threshold", DEFAULT_AD_THRESHOLD)
            if score >= threshold:
                action = ad_conf.get("action", "mute")
                try:
                    ob = self._ob(event)
                    await ob.call("delete_msg", message_id=event.message_obj.message_id)
                    if action == "ban":
                        await ob.call("set_group_kick", group_id=int(gid), user_id=int(uid), reject_add_request=True)
                    elif action == "kick":
                        await ob.call("set_group_kick", group_id=int(gid), user_id=int(uid))
                    else:  # mute
                        await ob.call("set_group_ban", group_id=int(gid), user_id=int(uid), duration=3600)
                    logger.info(f"{LOG} 广告拦截：群{gid} 用户{uid} 评分{score}，执行{action}")
                except Exception as e:
                    logger.warning(f"{LOG} 广告处理失败: {e}")

    async def terminate(self):
        """插件卸载清理。"""
        # 取消所有后台任务
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            import asyncio
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info(f"{LOG} 插件已卸载")
