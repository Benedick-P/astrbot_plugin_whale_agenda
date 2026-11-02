"""防撞车：避免和其他“识别主人 / 注入身份 / 压行”的模块互相踩。

冲突有三种，处理方式各不相同：

1. **重复注入**（最麻烦）：别的插件已经往 system prompt 写了「【当前发言人】」，
   或本插件被重复调用两次。做法是给自己的块打哨兵，重复调用只替换不叠加；
   检测到别人的块时保留它，但记一条告警，方便定位是谁在抢。
2. **主人 QQ 不一致**：别人的块里写了另一个 QQ 当主人。只告警不覆盖——
   谁是主人必须由本插件的绑定值决定。
3. **工具名重复**：另一个插件注册了同名 LLM 工具。启动时扫一遍注册表，
   发现同一个名字有多个提供方就告警。

所有告警都限流（同一条只记一次），避免每轮对话刷日志。
"""

import re

from astrbot.api import logger

SENTINEL = "【whale-owner-block】"
OWNER_TOOLS = (
    "whale_schedule_today",
    "whale_schedule_upcoming",
    "whale_schedule_day",
)

# 别的身份模块常见的标记：命中说明它也在写 system prompt
FOREIGN_MARKERS = (
    "【当前发言人】",
    "【主人】",
    "【发言人】",
    "is_owner",
    "当前说话人",
)

QQ_PATTERN = re.compile(r"QQ\s*[:：]?\s*(\d{5,12})")

_seen_warnings: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _seen_warnings:
        return
    _seen_warnings.add(key)
    logger.warning(f"[whale-safety] {message}")


def _strip_ours(prompt: str) -> str:
    """去掉本插件之前注入的块（哨兵之后到下一个空行为止）。"""
    if SENTINEL not in prompt:
        return prompt
    head, _, tail = prompt.partition(SENTINEL)
    rest = tail.split("\n\n", 1)
    return (head + (rest[1] if len(rest) > 1 else "")).rstrip()


def foreign_blocks(prompt: str) -> list[str]:
    """找出别人写的身份块（不含我们自己那段）。"""
    text = _strip_ours(prompt or "")
    found = []
    for marker in FOREIGN_MARKERS:
        start = text.find(marker)
        while start != -1:
            found.append(text[start:start + 240])
            start = text.find(marker, start + len(marker))
            break          # 每个标记只看第一处，够判断了
    return found


def inject(prompt: str, owner_qq: str, uid: str, name: str, enabled: bool = True) -> str:
    """注入主人的身份说明；重复调用只替换自己的块，不叠加。

    enabled=False 只关闭“检测别人是否也在注入”和相应告警，注入本身仍然执行。
    """
    from . import guard

    base = _strip_ours(prompt or "")
    note = guard.note_for(owner_qq, uid, name)
    block = f"{SENTINEL}\n{note}"

    if enabled:
        for foreign in foreign_blocks(base):
            _warn_once(
                f"foreign:{foreign[:40]}",
                "检测到其他模块也注入了身份说明，已保留它并追加本插件的块："
                f"{foreign[:60]!r}",
            )
            match = QQ_PATTERN.search(foreign)
            if match and match.group(1) != str(owner_qq):
                _warn_once(
                    f"qq:{match.group(1)}",
                    f"其他模块把 {match.group(1)} 当主人，但本插件绑定的是 {owner_qq}；"
                    "以本插件为准，建议停用其中一个",
                )

    return f"{base}\n\n{block}\n" if base else f"{block}\n"


def check_tools(context) -> list[str]:
    """启动时检查同名 LLM 工具是否被重复注册。"""
    problems = []
    try:
        funcs = getattr(context.provider_manager, "llm_tools", None)
        items = getattr(funcs, "func_list", []) or []
    except Exception:
        return problems

    seen: dict[str, str] = {}
    for tool in items:
        name = getattr(tool, "name", "")
        if name not in OWNER_TOOLS:
            continue
        origin = getattr(tool, "handler_module_path", "") or "unknown"
        if name in seen and seen[name] != origin:
            problems.append(name)
            _warn_once(
                f"tool:{name}:{origin}",
                f"LLM 工具 {name} 有多个提供方（{seen[name]} / {origin}），"
                "可能装了重复插件，建议只保留一个",
            )
        seen[name] = origin
    return problems


def reset_warnings() -> None:
    """测试用：清空告警去重表。"""
    _seen_warnings.clear()
