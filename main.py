"""小鲸鱼提醒插件：按周日程表向主人私聊推送定时提醒。

只发私聊，只认一个 QQ。任何群聊来源的命令都会被拒绝；发送目标写死为
`{platform}:FriendMessage:{owner_qq}`，不读消息事件里的会话，避免误发群聊。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import traceback
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import astrbot.api.star as star
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.message_event_result import MessageChain

from . import guard
from . import safety
from . import schedule_data as S
from . import tools as tool_logic
from . import voice
from .api import ApiServer
from .page_api import PageApi
from .store import Store

TICK_SECONDS = 20
STATE_KEEP_DAYS = 3


def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _minus_minutes(value: str, minutes: int) -> str:
    hour, minute = _hhmm(value)
    total = (hour * 60 + minute - minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


class WhaleReminder(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_whale_agenda")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()

        # 日程数据与代码分离：数据在 plugin_data，示例模板随插件分发
        self.plugin_dir = Path(__file__).resolve().parent
        self.store = Store(
            self.data_dir / "schedule.json",
            self.plugin_dir / "schedule.example.json",
        )
        self.store.load_or_init()
        S.bind(self.store)

        # WebUI 插件页面（views/schedule）的后端
        self.page_api = PageApi(self)
        self.page_api.register(self.context)

        self.api: ApiServer | None = None
        self.api_token = self._load_token()
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ---------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        self._stopping = False
        if self.opt("safety_guard", True):
            try:
                conflicts = safety.check_tools(self.context)
                logger.info(
                    "[whale-safety] 启动检查完成："
                    + ("未发现同名工具冲突" if not conflicts else f"冲突工具 {conflicts}")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[whale-safety] 启动检查失败: {exc}")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        self._start_api()
        logger.info(
            f"[whale-reminder] 已启动：主人 {self.owner_qq}，目标 {self.dm_umo()}，"
            f"人格生成 {'开' if self.voice_enabled else '关'}"
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """平台适配器就绪后再发上线通知：initialize 阶段还发不出去。"""
        if not self.opt("notify_on_start", True):
            return
        today = datetime.now(self.tz).date().isoformat()
        if self.state.get("hello_date") == today:
            return
        ok = await self._safe_send(
            dict(
                kind="hello",
                facts=[
                    f"今天是 {S.today_label(datetime.now(self.tz).date())}。",
                    "定时提醒已经接管：早间简报 07:15、课前 30 分钟、学习块开始、"
                    f"午晚饭、{S.LIGHTOUT_AT} 熄灯、周日 17:00 复盘。",
                    "全部只发这个私聊，不会进群。",
                ],
                fallback="我上线了，日程提醒从今天开始接管，只发这个私聊，不会跑去群里。",
            )
        )
        if ok:
            self.state["hello_date"] = today
            self._save_state()

    async def terminate(self) -> None:
        self._stopping = True
        if self.api is not None:
            self.api.stop()
            self.api = None
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[whale-reminder] 已停止")

    # ---------------------------------------------------------------- 配置

    @property
    def owner_qq(self) -> str:
        """主人 QQ：优先用 WebUI 页面绑定的值，没绑再用配置里的。"""
        bound = str(self.store.settings.get("owner_qq") or "").strip()
        return bound or str(self.opt("owner_qq", "")).strip()

    @property
    def guard_enabled(self) -> bool:
        return bool(self.opt("enable_owner_guard", True))

    @property
    def flatten_enabled(self) -> bool:
        return bool(self.opt("flatten_replies", True))

    def _is_owner_event(self, event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id() or "").strip() == self.owner_qq != ""
        except Exception:
            return False

    @property
    def platform_id(self) -> str:
        return str(self.opt("platform_id", "aiocqhttp")).strip() or "aiocqhttp"

    @property
    def tz(self) -> ZoneInfo:
        name = str(self.store.settings.get("timezone") or self.opt("timezone", "Asia/Shanghai"))
        return ZoneInfo(name)

    @property
    def semester_start(self):
        """学期第 1 周周一，来自 schedule.json 的 settings。"""
        return self.store.semester_start()

    @property
    def sleep_start(self):
        """作息调整第 1 周周一，来自 schedule.json 的 settings。"""
        return self.store.sleep_start()

    @property
    def voice_enabled(self) -> bool:
        return bool(self.opt("persona_voice", True))

    def opt(self, name: str, default):
        try:
            if self.config is None:
                return default
            value = self.config.get(name, default)
            return default if value is None else value
        except Exception:
            return default

    def dm_umo(self) -> str:
        return f"{self.platform_id}:FriendMessage:{self.owner_qq}"

    # ---------------------------------------------------------------- 状态

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state is not a dict")
            data.setdefault("sent", {})
            data.setdefault("silent_until", None)
            data.setdefault("enabled", True)
            data.setdefault("week_override", None)
            return data
        except Exception:
            return {"sent": {}, "silent_until": None, "enabled": True, "week_override": None}

    def _save_state(self) -> None:
        try:
            cutoff = (datetime.now(self.tz).date() - timedelta(days=STATE_KEEP_DAYS)).isoformat()
            self.state["sent"] = {
                k: v for k, v in self.state.get("sent", {}).items()
                if k.split("#", 1)[0] >= cutoff
            }
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[whale-reminder] 状态写入失败: {exc}")

    # ---------------------------------------------------------------- 日程 API

    def _load_token(self) -> str:
        """API token：配置里填了就用配置的，否则读文件，再否则生成并落盘。"""
        configured = str(self.opt("api_token", "") or "").strip()
        if configured:
            return configured
        path = self.data_dir / "api_token.txt"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_hex(16)
        path.write_text(token + "\n", encoding="utf-8")
        return token

    def _start_api(self) -> None:
        if not self.opt("api_enable", True):
            logger.info("[whale-reminder] 日程 API 已关闭（api_enable = false）")
            return
        host = str(self.opt("api_host", "127.0.0.1")).strip() or "127.0.0.1"
        try:
            port = int(self.opt("api_port", 6190) or 6190)
        except (TypeError, ValueError):
            port = 6190
        try:
            self.api = ApiServer(self.store, host, port, self.api_token)
            self.api.start()
            logger.info(
                f"[whale-reminder] 日程 API 已启动 {self.api.address}"
                f"（token 在 {self.data_dir / 'api_token.txt'}，请求头 X-Api-Token）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[whale-reminder] 日程 API 启动失败: {exc}")

    # ---------------------------------------------------------------- 调度

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[whale-reminder] tick 异常\n" + traceback.format_exc())
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self) -> None:
        if self.store.reload_if_changed():      # 外部改了 schedule.json 就热重载
            logger.info("[whale-reminder] schedule.json 已热重载")
        now = datetime.now(self.tz)
        if not self.state.get("enabled", True):
            return
        until = self.state.get("silent_until")
        if until:
            try:
                if now < datetime.fromisoformat(until):
                    return
                self.state["silent_until"] = None
                self._save_state()
            except ValueError:
                self.state["silent_until"] = None

        stamp = now.strftime("%H:%M")
        for item in self._reminders(now):
            if item["at"] != stamp:
                continue
            key = f"{now.date().isoformat()}#{item['kind']}#{item['at']}"
            if self.state["sent"].get(key):
                continue
            ok = await self._safe_send(item)
            if ok:
                self.state["sent"][key] = now.isoformat(timespec="seconds")
                self._save_state()

    def _sleep_week(self, day) -> int:
        """作息调整周次：和学期周次分开算，开始之前按第 1 周处理。"""
        return max(1, S.sleep_week_of(day, self.sleep_start))

    def _week(self, day) -> int:
        override = self.state.get("week_override")
        if override:
            try:
                return int(override)
            except (TypeError, ValueError):
                pass
        return S.week_of(day, self.semester_start)

    def _reminders(self, now: datetime) -> list[dict]:
        day = now.date()
        wd = now.weekday()
        week = self._week(day)
        if week < 1:
            return []

        courses = S.courses_on(wd, week)
        blocks = S.blocks_on(wd, week)
        sleep = S.sleep_for(self._sleep_week(day))
        out: list[dict] = []

        if self.opt("enable_briefing", True):
            facts = [
                f"今天是 {S.today_label(day)}，作息调整第 {self._sleep_week(day)} 周。"
            ]
            if courses:
                facts.append("今天的课：" + "；".join(
                    f"{c['span']} {c['name']}（{c['room']}）" for c in courses))
            else:
                facts.append("今天没有课。")
            study = [b for b in blocks if b["label"] != "周复盘"]
            if study:
                facts.append("学习块：" + "；".join(f"{b['start']} {b['label']}" for b in study))
            facts.append(
                f"作息：{sleep['wake_class']} 起床（无早课 {sleep['wake_late']}），"
                f"午休 {sleep['nap']} 分钟，今晚 {sleep['bedtime']} 熄灯。"
            )
            facts.append("早饭现在吃；午饭 11:35，晚饭 17:35。")
            out.append(dict(
                at="07:15",
                kind="briefing",
                facts=facts,
                length="80-140 字",
                fallback=self._brief_note(day, week, courses, study, sleep),
            ))

        if self.opt("enable_class", True):
            for c in courses:
                if c["start"] <= "08:30":
                    continue          # 早课已经由 07:15 简报覆盖
                out.append(dict(
                    at=_minus_minutes(c["start"], 30),
                    kind="class",
                    facts=[
                        f"还有 30 分钟上课：{c['span']} {c['name']}，地点 {c['room']}。",
                        "提醒主人现在收东西出发，别卡着点跑。",
                    ],
                    fallback=f"还有半小时，{c['span']} 的{c['name']}在{c['room']}，该收东西了。",
                ))

        if self.opt("enable_study", True):
            for b in blocks:
                if b["label"] == "周复盘":
                    continue
                facts = [f"学习块现在开始：{b['span']} {b['label']}。"]
                if b["reason"]:
                    facts.append(f"这一块的做法：{b['reason']}。")
                out.append(dict(
                    at=b["start"],
                    kind="study",
                    facts=facts,
                    fallback=f"{b['label']} 时间到（{b['span']}）" +
                             (f"，{b['reason']}" if b["reason"] else "") + "。",
                ))

        if self.opt("enable_meal", True):
            for meal in S.MEALS:
                if meal["at"] == "07:15":
                    continue          # 早饭并入简报
                out.append(dict(
                    at=meal["at"],
                    kind="meal",
                    facts=[f"{meal['label']}时间到了。{meal['hint']}。"],
                    fallback=f"{meal['label']}时间到，{meal['hint']}。",
                ))

        if self.opt("enable_lightout", True):
            out.append(dict(
                at=S.LIGHTOUT_AT,
                kind="lightout",
                facts=[
                    f"23:00 是熄灯线。本周目标入睡时间 {sleep['bedtime']}，"
                    f"早上 {sleep['wake_class']} 起。",
                    "提醒主人现在停下手上的事，洗漱、读几页纸书，不要开新任务。",
                ],
                fallback=f"23:00 了，本周目标是 {sleep['bedtime']} 睡。先停手，洗漱，别开新任务。",
            ))

        if self.opt("enable_review", True) and wd == 6:
            out.append(dict(
                at="17:00",
                kind="review",
                facts=[
                    f"周日复盘时间（{S.today_label(day)}）。",
                    "要回答：三条学习线各完成多少小时；本周实际几点熄灯、超了几次；"
                    "Rust 本周的产出能不能一句话说清；下周有没有特殊周次；删掉一件下周不打算做的事。",
                ],
                fallback="周日复盘了：三条线各多少小时、这周几点睡、Rust 的产出是什么、下周删一件事。",
            ))

        return out

    def _brief_note(self, day, week, courses, blocks, sleep) -> str:
        head = f"第 {week} 周 周{S.WEEKDAY_NAMES[day.weekday()]}。"
        if courses:
            body = "今天的课：" + "；".join(
                f"{c['start']} {c['name']}（{c['room']}）" for c in courses) + "。"
        else:
            body = "今天没课。"
        study = ("学习块：" + "；".join(f"{b['start']} {b['label']}" for b in blocks) + "。") if blocks else ""
        return f"{head}{body}{study}午休 {sleep['nap']} 分钟，今晚 {sleep['bedtime']} 熄灯。"

    # ---------------------------------------------------------------- 发送

    async def _safe_send(self, item: dict) -> bool:
        umo = self.dm_umo()
        try:
            text = await voice.compose(
                self.context,
                umo,
                item["facts"],
                item["fallback"],
                self.voice_enabled,
                length=item.get("length", "40-90 字"),
            )
        except Exception as exc:
            logger.warning(f"[whale-reminder] 生成失败，改用兜底：{exc}")
            text = item["fallback"]
        try:
            result = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:
            logger.error(f"[whale-reminder] 发送失败 kind={item['kind']}: {exc}")
            return False
        if result is False:
            logger.error(f"[whale-reminder] 未找到平台，消息未送达：{umo}")
            return False
        logger.info(f"[whale-reminder] 已发送 kind={item['kind']} -> {umo}｜{text[:60]}")
        return True

    # ---------------------------------------------------------------- 身份注入

    @filter.on_llm_request()
    async def inject_owner_identity(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """把当前发言人身份写进 system prompt，人格卡的主人文案才认得出来。"""
        if not self.guard_enabled or not self.owner_qq:
            return
        try:
            uid = str(event.get_sender_id() or "").strip()
            name = (event.get_sender_name() or "").strip() or "未知"
        except Exception:
            return
        if not uid:
            return
        req.system_prompt = safety.inject(
            req.system_prompt, self.owner_qq, uid, name,
            enabled=bool(self.opt("safety_guard", True)),
        )
        guard.log_injection(self.owner_qq, uid, name)

    @filter.on_llm_response()
    async def flatten_reply(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """把多段回复压成一行，换行改标点。"""
        if not self.flatten_enabled:
            return
        chain = getattr(response, "result_chain", None)
        components = getattr(chain, "chain", None) if chain is not None else None
        if not components:
            return
        changed = 0
        for comp in components:
            if not isinstance(comp, Plain):
                continue
            text = getattr(comp, "text", None)
            if not text:
                continue
            flat = guard.flatten(text)
            if flat != text:
                comp.text = flat
                changed += 1
        if changed:
            logger.info(f"[whale-guard] 压行 {changed} 段")

    # ---------------------------------------------------------------- LLM 工具

    PRIVATE_TOOL_REPLY = "这是主人的私人日程，不能提供。"

    @llm_tool(name="whale_schedule_today")
    async def tool_schedule_today(self, event: AstrMessageEvent) -> str:
        """查询主人今天的日程：第几周、今天的课与教室、学习块、吃饭时间、今晚几点熄灯。

        当主人问“今天有什么安排”“今天有课吗”“现在该干什么”“晚上几点睡”
        或聊到作息、学习计划、上课地点时调用。

        Returns:
            今天的日程文本；非主人调用或数据不可用时返回说明。
        """
        if not self._is_owner_event(event):
            return self.PRIVATE_TOOL_REPLY
        try:
            return tool_logic.today_text(S, self.store, datetime.now(self.tz))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[whale-reminder] 工具 today 失败: {exc}")
            return "查日程时出错了，稍后再试。"

    @llm_tool(name="whale_schedule_upcoming")
    async def tool_schedule_upcoming(
        self, event: AstrMessageEvent, hours: int = 12
    ) -> str:
        """查询主人接下来若干小时的日程节点（起床、上课、学习块、吃饭、熄灯），可跨天。

        当主人问“接下来干嘛”“几点有课”“这两天怎么安排”“下周有什么课”时调用。
        窗口跨天时按日期分段，并标注今天/明天。

        Args:
            hours(number): 向后查询的小时数，1-168（最多 7 天），默认 12。

        Returns:
            时间段内的节点列表；窗口内没有安排时说明下一件事。
        """
        if not self._is_owner_event(event):
            return self.PRIVATE_TOOL_REPLY
        try:
            return tool_logic.upcoming_text(S, self.store, datetime.now(self.tz), hours)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[whale-reminder] 工具 upcoming 失败: {exc}")
            return "查日程时出错了，稍后再试。"

    @llm_tool(name="whale_schedule_day")
    async def tool_schedule_day(self, event: AstrMessageEvent, day: str = "今天") -> str:
        """查询主人指定某一天的全部安排（起床、课表、教室、学习块、吃饭、熄灯）。

        当主人问“明天有什么课”“周三怎么安排”“下周三有课吗”“10 月 30 日有事吗”时调用。

        Args:
            day(string): 要查的日期，可填 今天、明天、后天、下周三、周五，或 2026-10-30、10-30。

        Returns:
            该天的完整安排；非主人调用或日期无法解析时返回说明。
        """
        if not self._is_owner_event(event):
            return self.PRIVATE_TOOL_REPLY
        try:
            return tool_logic.day_text(S, self.store, datetime.now(self.tz), day)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[whale-reminder] 工具 day 失败: {exc}")
            return "查日程时出错了，稍后再试。"

    # ---------------------------------------------------------------- 命令

    @filter.command("提醒")
    async def cmd_remind(self, event: AstrMessageEvent):
        async for reply in self._handle_command(event):
            yield reply

    @filter.command("remind")
    async def cmd_remind_en(self, event: AstrMessageEvent):
        async for reply in self._handle_command(event):
            yield reply

    async def _handle_command(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id() or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if sender != self.owner_qq:
            logger.info(f"[whale-reminder] 拒绝非主人命令 sender={sender}")
            yield event.plain_result("这个只在主人那里生效。")
            return
        if ":FriendMessage:" not in umo:
            yield event.plain_result("提醒只在私聊里设置，群聊里不处理，也不会往群里发。")
            return

        raw = (event.message_str or "").strip()
        for prefix in ("/提醒", "提醒", "/remind", "remind"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        action = raw or "状态"

        if action in ("状态", "status"):
            now = datetime.now(self.tz)
            week = self._week(now.date())
            upcoming = [f"{r['at']} {r['kind']}" for r in self._reminders(now)
                        if r["at"] >= now.strftime("%H:%M")][:5]
            silent = self.state.get("silent_until")
            lines = [
                f"第 {week} 周 · 开关 {'开' if self.state.get('enabled', True) else '关'} · "
                f"人格 {'开' if self.voice_enabled else '关'}",
                f"目标：{self.dm_umo()}",
                "接下来：" + ("、".join(upcoming) if upcoming else "今天没有了"),
                f"静默至：{silent or '无'}",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("测试", "test"):
            now = datetime.now(self.tz)
            items = [r for r in self._reminders(now) if r["kind"] == "briefing"]
            item = items[0] if items else dict(
                kind="test",
                facts=[f"今天 {S.today_label(now.date())}，这是一条测试提醒。"],
                fallback="测试一下，能收到就说明链路是通的。",
            )
            ok = await self._safe_send(item)
            yield event.plain_result("发出去了，看看收到没有。" if ok else "没发出去，看日志。")
            return

        if action.startswith("静默"):
            arg = action.replace("静默", "").strip()
            try:
                hours = float(arg)
            except ValueError:
                hours = 1.0
            until = datetime.now(self.tz) + timedelta(hours=hours)
            self.state["silent_until"] = until.isoformat(timespec="seconds")
            self._save_state()
            yield event.plain_result(f"静默到 {until.strftime('%m-%d %H:%M')}。")
            return

        if action in ("关", "off"):
            self.state["enabled"] = False
            self._save_state()
            yield event.plain_result("提醒已关。开回来发「提醒 开」。")
            return

        if action in ("开", "on"):
            self.state["enabled"] = True
            self._save_state()
            yield event.plain_result("提醒已开。")
            return

        if action.startswith("周次"):
            arg = action.replace("周次", "").strip()
            if arg in ("", "自动", "reset"):
                self.state["week_override"] = None
                self._save_state()
                yield event.plain_result("周次改回按开学日期自动计算。")
            else:
                try:
                    self.state["week_override"] = int(arg)
                    self._save_state()
                    yield event.plain_result(f"周次强制为第 {arg} 周。")
                except ValueError:
                    yield event.plain_result("用法：提醒 周次 9")
            return

        yield event.plain_result(
            "用法：提醒 状态 / 提醒 测试 / 提醒 静默 2 / 提醒 开 / 提醒 关 / 提醒 周次 9"
        )
