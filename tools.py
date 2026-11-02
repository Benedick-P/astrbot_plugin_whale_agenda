"""日程查询的纯逻辑：给 LLM 工具用，不依赖 AstrBot。

从桥接插件搬进来，改成直接读 Store，不再靠 sys.modules 找兄弟模块。
所有函数返回人类可读字符串，异常由调用方兜。
"""

from datetime import date, datetime, timedelta

KIND_LABEL = {
    "wake": "起床",
    "class": "上课",
    "study": "学习块",
    "meal": "吃饭",
    "lightout": "熄灯",
    "review": "复盘",
}

MAX_HOURS = 168          # 7 天
WEEKDAY = "一二三四五六日"


def parse_day(raw: str, today: date):
    """今天/明天/后天/周X/下周X/2026-10-30；解析不了返回 None。"""
    import re

    text = (raw or "").strip()
    keys = {"今天": 0, "today": 0, "明天": 1, "明日": 1, "tomorrow": 1,
            "后天": 2, "大后天": 3}
    if not text:
        return today
    if text in keys:
        return today + timedelta(days=keys[text])

    weekday = re.match(r"^(下|本|这|上)?(?:周|星期|礼拜)([一二三四五六日天])$", text)
    if weekday:
        index = WEEKDAY.find(weekday.group(2).replace("天", "日"))
        base = today.weekday()
        qualifier = weekday.group(1)
        if qualifier == "下":
            delta = (7 - base) + index
        elif qualifier in ("本", "这"):
            delta = index - base
        elif qualifier == "上":
            delta = index - base - 7
        else:
            delta = index - base if index >= base else index - base + 7
        return today + timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%m-%d", "%Y/%m/%d", "%m/%d"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=today.year)
            return parsed
        except ValueError:
            continue
    return None


def timeline(module, store, day) -> list:
    """某天的完整时间线：[(时刻, 类型, 标题, 节次), ...]，含早课与起床。"""
    week = module.week_of(day, store.semester_start())
    if week < 1:
        return []
    rows = []
    try:
        early = module.has_early_class(day.weekday(), week)
        phase = module.sleep_for(store_sleep_week(module, store, day))
        rows.append((phase["wake_class"] if early else phase["wake_late"],
                     "wake", "该起床了", ""))
    except Exception:
        pass

    for course in module.courses_on(day.weekday(), week):
        rows.append((course["start"], "class",
                     f"{course['name']}（{course['room']}）", course["span"]))
    for block in module.blocks_on(day.weekday(), week):
        rows.append((block["start"], "study", block["label"], block["span"]))
    for meal in store.meals:
        if meal["at"] == "07:15":
            continue
        rows.append((meal["at"], "meal", meal["label"], ""))
    rows.append((store.lightout_at(), "lightout", "该熄灯了", ""))
    if day.weekday() == 6:
        rows.append(("17:00", "review", "周复盘", ""))

    out = []
    for stamp, kind, title, span in sorted(rows, key=lambda row: row[0]):
        hour, minute = (int(part) for part in stamp.split(":"))
        when = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        out.append((when, kind, title, span))
    return out


def store_sleep_week(module, store, day) -> int:
    """作息调整周次，开始前按第 1 周算。"""
    return max(1, module.sleep_week_of(day, store.sleep_start()))


def line(when: datetime, kind: str, title: str, span: str) -> str:
    head = span if span else f"{when:%H:%M}"
    return f"{head} {KIND_LABEL.get(kind, kind)}｜{title}"


def day_summary(module, store, day) -> str:
    """当天摘要，复用提醒插件的简报文案。"""
    week = module.week_of(day, store.semester_start())
    if week < 1:
        return "还没开学。"
    courses = module.courses_on(day.weekday(), week)
    blocks = module.blocks_on(day.weekday(), week)
    head = f"第 {week} 周 周{WEEKDAY[day.weekday()]}。"
    if courses:
        body = "今天的课：" + "；".join(
            f"{c['start']} {c['name']}（{c['room']}）" for c in courses) + "。"
    else:
        body = "今天没课。"
    study = ("学习块：" + "；".join(f"{b['start']} {b['label']}" for b in blocks) + "。") if blocks else ""
    phase = module.sleep_for(store_sleep_week(module, store, day))
    return f"{head}{body}{study}今晚 {phase['bedtime']} 熄灯。"


def today_text(module, store, now: datetime) -> str:
    now = now.replace(tzinfo=None)  # 内部全部用 naive 时间比较
    lines = [f"现在 {now:%H:%M}。", day_summary(module, store, now.date())]
    remaining = [row for row in timeline(module, store, now.date()) if row[0] > now]
    if remaining:
        lines.append("下一件事：" + line(*remaining[0]))
    else:
        tomorrow = timeline(module, store, (now + timedelta(days=1)).date())
        if tomorrow:
            lines.append("今天到此为止。明天第一件事：" + line(*tomorrow[0]))
        else:
            lines.append("今天到此为止，明天也没有安排。")
    return "\n".join(lines)


def day_head(day: date, today: date) -> str:
    label = f"{day:%m-%d} 周{WEEKDAY[day.weekday()]}"
    if day == today:
        return f"— {label}（今天）—"
    if day == today + timedelta(days=1):
        return f"— {label}（明天）—"
    return f"— {label} —"


def upcoming_text(module, store, now: datetime, hours: int) -> str:
    now = now.replace(tzinfo=None)  # 内部全部用 naive 时间比较
    span = max(1, min(int(hours or 12), MAX_HOURS))
    end = now + timedelta(hours=span)
    today = now.date()
    picked = []
    for offset in range(0, span // 24 + 2):
        for row in timeline(module, store, today + timedelta(days=offset)):
            if now < row[0] <= end:
                picked.append(row)
    if not picked:
        tomorrow = timeline(module, store, today + timedelta(days=1))
        if tomorrow:
            return f"接下来 {span} 小时没有安排。下一件事：{line(*tomorrow[0])}"
        return f"接下来 {span} 小时没有安排。"

    picked.sort(key=lambda row: row[0])
    lines = [f"接下来 {span} 小时（现在 {now:%m-%d %H:%M}）："]
    current = None
    for row in picked:
        day = row[0].date()
        if day != current:
            current = day
            lines.append(day_head(day, today))
        lines.append(line(*row))
    return "\n".join(lines)


def day_text(module, store, now: datetime, raw: str) -> str:
    now = now.replace(tzinfo=None)  # 内部全部用 naive 时间比较
    target = parse_day(raw, now.date())
    if target is None:
        return "没看懂这个日期，用「明天」「后天」「下周三」或 2026-10-30 这种写法。"
    week = module.week_of(target, store.semester_start())
    if week < 1:
        return f"{target:%m-%d} 还没开学，没有课表。"
    if week > 20:
        return f"{target:%m-%d} 已经是第 {week} 周，超出课表范围（第 1-16 周）。"
    rows = timeline(module, store, target)
    head = f"{target:%m-%d} 周{WEEKDAY[target.weekday()]}（第 {week} 周）"
    if not rows:
        return f"{head}没有安排。"
    return "\n".join([head + "："] + [line(*row) for row in rows])
