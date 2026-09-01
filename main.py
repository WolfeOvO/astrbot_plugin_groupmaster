"""
AstrBot 群管插件 astrbot_plugin_groupmaster v1.0.6

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

OneBot(NapCat) 动作：set_group_ban / set_group_kick / delete_msg /
set_group_whole_ban / set_group_admin / set_group_add_request /
get_group_member_info / get_group_list / get_group_msg_history / get_group_setting /
get_group_shut_list
"""

import json
import logging
import os
import re
import time
from typing import Optional, Tuple

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")
LOG = "[groupmaster]"

COMMAND_NAMES = {"timeout", "kick", "ban", "unban", "warn", "recall", "mute", "admin", "status"}
OP_NAMES = {
    "timeout": "禁言",
    "kick": "踢出",
    "ban": "拉黑踢出",
    "unban": "解除拉黑",
    "warn": "警告",
    "recall": "撤回",
    "mute": "全员禁言",
    "admin": "管理员设置",
    "status": "状态查询",
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
    "1.0.6",
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
    async def _do_timeout(self, event, gid, target: str, dur: int) -> Tuple[bool, str]:
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
            self._save_state()
        except Exception:
            pass
        if dur <= 0:
            return True, f"已解除 {target} 的禁言"
        return True, f"已禁言 {target} {dur} 秒"

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
            return True, f"已将 {target} 移出群聊"
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
                    lines.append(f"🔇 禁言中：剩余 {t}")
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
                    lines.append(f"🔇 {uid} 禁言剩余 {t}")
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
                return False, "用法：timeout <秒数> <@用户/QQ号>；timeout 0 <@用户> = 解除其禁言"
            return await self._do_timeout(event, gid, target, dur)
        if op == "kick":
            target = extract_target_qq(event, toks)
            if not target:
                if chain_has_at_self(event):
                    return False, "不能对机器人自己执行踢出。若要踢他人：kick <@用户/QQ号>"
                return False, "用法：kick <@用户/QQ号>"
            return await self._do_kick(event, gid, target, blacklist=False)
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
    async def tool_timeout(self, event: AstrMessageEvent, user_id: str, duration_sec: int = 600):
        """禁言当前群内指定用户。user_id 为目标 QQ 号（从消息中的 @昵称(QQ号) 或用户提供的号码解析），duration_sec 为禁言秒数（0~2592000，0 表示解除禁言）。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_timeout(event, gid, str(user_id), max(0, min(int(duration_sec), 2592000)))
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        return ("✅ " if ok else "❌ ") + msg

    @llm_tool(name="gm_kick_user")
    async def tool_kick(self, event: AstrMessageEvent, user_id: str):
        """将当前群内指定用户移出群聊（不拉黑）。user_id 为目标 QQ 号。仅群聊可用。"""
        gid = self._llm_group_gate(event)
        if not gid:
            return "该操作仅能在群聊中使用。"
        err = await self._llm_perm_gate(event)
        if err:
            return err
        try:
            ok, msg = await self._do_kick(event, gid, str(user_id), blacklist=False)
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

    async def terminate(self):
        """插件卸载清理。"""
        logger.info(f"{LOG} 插件已卸载")
