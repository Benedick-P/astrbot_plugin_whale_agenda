"""把小鲸鱼人格接进提醒：优先用当前人格生成，失败就退回固定文案。

调用链与 AstrBot 主动消息插件一致：
    provider = context.get_using_provider(umo)
    persona  = await context.persona_manager.get_default_persona_v3(umo)
    resp     = await provider.text_chat(prompt=..., system_prompt=...)
"""

import asyncio

from astrbot.api import logger

INSTRUCTION = (
    "\n\n【本轮任务】你要给主人发一条定时提醒。"
    "把下面的事实用你自己的口吻讲给他听：{length}，一条消息，不分段，"
    "不用 markdown、不加表情、不用引号包裹，不写“提醒你”这种模板句，"
    "不要编造事实里没有的课程、时间或地点，也不要提问。"
)

MAX_WAIT = 25


def _clean(text: str) -> str:
    text = (text or "").strip().strip('"“”')
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return text[:400]


async def compose(
    context,
    umo: str,
    facts,
    fallback: str,
    enabled: bool = True,
    length: str = "40-90 字",
) -> str:
    """生成一条提醒正文。失败一律返回 fallback，保证提醒不会因为模型挂掉而消失。"""
    if not enabled:
        return fallback

    provider = None
    try:
        provider = context.get_using_provider(umo=umo)
    except Exception as exc:
        logger.warning(f"[whale-reminder] 取 provider 失败: {exc}")
    if provider is None:
        return fallback

    system_prompt = ""
    try:
        persona = await context.persona_manager.get_default_persona_v3(umo)
        system_prompt = getattr(persona, "system_prompt", "") or ""
    except Exception as exc:
        logger.warning(f"[whale-reminder] 取人格失败: {exc}")

    try:
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt="\n".join(facts),
                session_id=umo,
                contexts=[],
                system_prompt=system_prompt + INSTRUCTION.format(length=length),
            ),
            timeout=MAX_WAIT,
        )
        text = _clean(getattr(resp, "completion_text", "") or "")
        if text:
            return text
    except asyncio.TimeoutError:
        logger.warning("[whale-reminder] 模型超时，用兜底文案")
    except Exception as exc:
        logger.warning(f"[whale-reminder] 模型调用失败: {exc}")

    return fallback
