"""日程数据的加载、校验与原子保存。

数据与代码分离：课表、自习块、三餐、作息表全部放在
`plugin_data/astrbot_plugin_whale_agenda/schedule.json`，改文件或走 HTTP API
都改这一份。这样插件本身不含任何个人信息，可以直接分享给别人。

设计要点：
- 写入走临时文件 + 原子替换，写一半断电不会留下半个 JSON。
- 校验失败抛 ScheduleError，调用方拿到人话错误，旧数据保持不变。
- 热重载按 mtime 判断，改完文件最多 20 秒（一个 tick）生效，不用重启。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_MIN, DAY_MAX = 0, 6
WEEKDAY_NAMES = "一二三四五六日"

DEFAULT_SETTINGS = {
    "owner_qq": "",
    "semester_start": "2026-08-31",
    "sleep_start": "2026-08-31",
    "lightout_at": "23:00",
    "timezone": "Asia/Shanghai",
}


class ScheduleError(ValueError):
    """日程数据不合法。消息直接给人看。"""


def _need(cond, message):
    if not cond:
        raise ScheduleError(message)


def _check_hhmm(value, where):
    _need(isinstance(value, str) and bool(HHMM.match(value)), f"{where} 必须是 HH:MM 格式，收到 {value!r}")


def _check_day(value, where):
    _need(isinstance(value, int) and DAY_MIN <= value <= DAY_MAX,
          f"{where} 的 day 必须是 0-6 的整数（0=周一），收到 {value!r}")


def _check_weeks(value, where):
    _need(isinstance(value, list), f"{where} 的 weeks 必须是列表")
    out = []
    for pair in value:
        _need(isinstance(pair, (list, tuple)) and len(pair) == 2,
              f"{where} 的 weeks 每一项必须是 [起始周, 结束周]")
        start, end = int(pair[0]), int(pair[1])
        _need(start >= 1 and end >= start, f"{where} 的周次区间不合法：{pair!r}")
        out.append([start, end])
    return out


def _check_date(value, where):
    _need(isinstance(value, str), f"{where} 必须是 YYYY-MM-DD 字符串")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ScheduleError(f"{where} 不是合法日期：{value!r}") from exc


def _assign_ids(items):
    for item in items:
        if not isinstance(item.get("id"), str) or not item["id"]:
            item["id"] = uuid.uuid4().hex[:8]
    return items


def normalize(data: dict) -> dict:
    """校验并补全字段，返回可直接保存的数据。不合法时抛 ScheduleError。"""
    _need(isinstance(data, dict), "日程数据必须是 JSON 对象")

    settings = dict(DEFAULT_SETTINGS)
    settings.update(data.get("settings") or {})
    _check_date(settings["semester_start"], "settings.semester_start")
    _check_date(settings["sleep_start"], "settings.sleep_start")
    _check_hhmm(settings["lightout_at"], "settings.lightout_at")
    settings["owner_qq"] = str(settings.get("owner_qq") or "").strip()
    if not isinstance(settings.get("timezone"), str) or not settings["timezone"]:
        settings["timezone"] = DEFAULT_SETTINGS["timezone"]

    courses = []
    for index, course in enumerate(data.get("courses") or []):
        where = f"第 {index + 1} 节课"
        _need(isinstance(course, dict), f"{where} 必须是对象")
        _check_day(course.get("day"), where)
        _check_hhmm(course.get("start"), where)
        name = str(course.get("name") or "").strip()
        _need(name, f"{where} 缺少课程名 name")
        courses.append(dict(
            id=course.get("id"),
            day=course["day"],
            start=course["start"],
            span=str(course.get("span") or course["start"]).strip(),
            name=name,
            room=str(course.get("room") or "").strip(),
            weeks=_check_weeks(course.get("weeks") or [[1, 16]], where),
        ))

    blocks = []
    for index, block in enumerate(data.get("blocks") or []):
        where = f"第 {index + 1} 个自习块"
        _need(isinstance(block, dict), f"{where} 必须是对象")
        _check_day(block.get("day"), where)
        _check_hhmm(block.get("start"), where)
        label = str(block.get("label") or "").strip()
        _need(label, f"{where} 缺少名称 label")
        blocks.append(dict(
            id=block.get("id"),
            day=block["day"],
            start=block["start"],
            span=str(block.get("span") or block["start"]).strip(),
            label=label,
            reason=str(block.get("reason") or "").strip(),
            skip_weeks=_check_weeks(block.get("skip_weeks") or [], where),
        ))

    meals = []
    for index, meal in enumerate(data.get("meals") or []):
        where = f"第 {index + 1} 顿饭"
        _need(isinstance(meal, dict), f"{where} 必须是对象")
        _check_hhmm(meal.get("at"), where)
        label = str(meal.get("label") or "").strip()
        _need(label, f"{where} 缺少名称 label")
        meals.append(dict(
            id=meal.get("id"),
            at=meal["at"],
            label=label,
            hint=str(meal.get("hint") or "").strip(),
        ))

    plan = []
    for index, phase in enumerate(data.get("sleep_plan") or []):
        where = f"第 {index + 1} 段作息"
        _need(isinstance(phase, dict), f"{where} 必须是对象")
        try:
            start, end = int(phase["from_week"]), int(phase["to_week"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError(f"{where} 需要 from_week 与 to_week 整数") from exc
        _need(start >= 1 and end >= start, f"{where} 的周次区间不合法")
        _check_hhmm(phase.get("bedtime"), where)
        _check_hhmm(phase.get("wake_class"), where)
        _check_hhmm(phase.get("wake_late"), where)
        plan.append(dict(
            id=phase.get("id"),
            from_week=start,
            to_week=end,
            bedtime=phase["bedtime"],
            wake_class=phase["wake_class"],
            wake_late=phase["wake_late"],
            nap=int(phase.get("nap") or 0),
        ))
    plan.sort(key=lambda item: item["from_week"])
    _assign_ids(plan)

    _need(courses or blocks, "日程里至少要有一节课或一个自习块")

    return {
        "version": 1,
        "settings": settings,
        "courses": _assign_ids(courses),
        "blocks": _assign_ids(blocks),
        "meals": _assign_ids(meals),
        "sleep_plan": plan,
    }


class Store:
    """一份 schedule.json 的内存视图 + 原子写 + 热重载。"""

    def __init__(self, path: Path, example: Path):
        self.path = Path(path)
        self.example = Path(example)
        self._lock = threading.RLock()
        self._mtime = 0.0
        self.data: dict = {"settings": dict(DEFAULT_SETTINGS), "courses": [],
                           "blocks": [], "meals": [], "sleep_plan": []}

    # ------------------------------------------------------------------ 读写

    def load_or_init(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            if self.example.exists():
                self.path.write_text(self.example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.save(self.data)
        self.load()
        return self.data

    def load(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        data = normalize(raw)
        with self._lock:
            self.data = data
            self._mtime = self.path.stat().st_mtime
        return data

    def reload_if_changed(self) -> bool:
        """文件被改过就重新加载；加载失败保留旧数据，不让插件倒下。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._mtime:
            return False
        try:
            self.load()
        except Exception:
            self._mtime = mtime        # 别每次 tick 都重试同一个坏文件
            return False
        return True

    def save(self, data: dict) -> dict:
        data = normalize(data)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)
        with self._lock:
            self.data = data
            self._mtime = self.path.stat().st_mtime
        return data

    # ------------------------------------------------------------------ 便捷读取

    @property
    def settings(self) -> dict:
        with self._lock:
            return dict(self.data.get("settings") or DEFAULT_SETTINGS)

    @property
    def courses(self) -> list:
        with self._lock:
            return list(self.data.get("courses") or [])

    @property
    def blocks(self) -> list:
        with self._lock:
            return list(self.data.get("blocks") or [])

    @property
    def meals(self) -> list:
        with self._lock:
            return list(self.data.get("meals") or [])

    @property
    def sleep_plan(self) -> list:
        with self._lock:
            return list(self.data.get("sleep_plan") or [])

    def semester_start(self) -> date:
        return datetime.strptime(self.settings["semester_start"], "%Y-%m-%d").date()

    def sleep_start(self) -> date:
        return datetime.strptime(self.settings["sleep_start"], "%Y-%m-%d").date()

    def lightout_at(self) -> str:
        return self.settings["lightout_at"]
