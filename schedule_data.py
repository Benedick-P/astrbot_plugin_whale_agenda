"""日程数据的只读适配层。

真正的数据在 `plugin_data/astrbot_plugin_whale_agenda/schedule.json`，
由 `store.Store` 持有。这里只提供函数式访问，供提醒调度和桥接插件调用。

模块级 `__getattr__` 让 `schedule_data.MEALS`、`.LIGHTOUT_AT`、`.SEMESTER_START`
这类“属性”始终跟着最新数据走，避免热重载后拿到旧值。
"""

from __future__ import annotations

from datetime import date

WEEKDAY_NAMES = "一二三四五六日"

_store = None


def bind(store) -> None:
    """由插件在启动时注入 Store 实例。"""
    global _store
    _store = store


def get_store():
    return _store


def __getattr__(name):
    # 只在模块里确实没有这个名字时才会走到这里
    if name == "MEALS":
        return _store.meals if _store else []
    if name == "LIGHTOUT_AT":
        return _store.lightout_at() if _store else "23:00"
    if name == "SEMESTER_START":
        return _store.semester_start() if _store else date(2026, 8, 31)
    if name == "SLEEP_START":
        return _store.sleep_start() if _store else date(2026, 8, 31)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def week_of(day: date, start: date) -> int:
    """学期周次；开学前返回 0，超过 16 周继续顺延。"""
    delta = (day - start).days
    return delta // 7 + 1 if delta >= 0 else 0


def sleep_week_of(day: date, start: date) -> int:
    """作息调整的周次，独立于学期周次；开始之前返回 0。"""
    delta = (day - start).days
    return delta // 7 + 1 if delta >= 0 else 0


def _in_weeks(week: int, ranges) -> bool:
    return any(a <= week <= b for a, b in ranges)


def courses_on(day_index: int, week: int):
    if _store is None:
        return []
    return [c for c in _store.courses
            if c["day"] == day_index and _in_weeks(week, c["weeks"])]


def blocks_on(day_index: int, week: int):
    if _store is None:
        return []
    out = []
    for block in _store.blocks:
        if block["day"] != day_index:
            continue
        if _in_weeks(week, block.get("skip_weeks") or []):
            continue
        out.append(block)
    return out


def sleep_for(week: int):
    if _store is None:
        return dict(bedtime="00:00", wake_class="07:15", wake_late="07:30", nap=25)
    plan = _store.sleep_plan
    for phase in plan:
        if phase["from_week"] <= week <= phase["to_week"]:
            return phase
    return plan[-1] if plan else dict(
        bedtime="00:00", wake_class="07:15", wake_late="07:30", nap=25)


def has_early_class(day_index: int, week: int) -> bool:
    return any(course["start"] <= "08:30" for course in courses_on(day_index, week))


def today_label(day: date) -> str:
    start = _store.semester_start() if _store else date(2026, 8, 31)
    return f"第 {week_of(day, start)} 周 周{WEEKDAY_NAMES[day.weekday()]}"
