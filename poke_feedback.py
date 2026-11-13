"""被戳一戳时，用框架人格生成一句回应。"""

import time
import asyncio
import random

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

sv = SV("戳一戳", pm=6, area="ALL", aside=False)

_COOLDOWN_SECONDS = 20.0
_AI_TIMEOUT = 25.0
_MAX_CONCURRENCY = 2
_MAX_COOLDOWN_KEYS = 2048
_MAX_REPLY_CHARS = 120

_FALLBACKS = (
    "戳我干嘛，小心戳漏气了。",
    "别戳啦，再戳就要弹回来咯。",
    "收到一下戳，正在假装很疼。",
    "嗯？找我有事就直接说嘛。",
    "再戳就把你记在小本本上。",
)

_last_reply: dict[str, float] = {}
_semaphore: asyncio.Semaphore | None = None


def _ai_slots() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _semaphore


def _cooldown_key(ev: Event) -> str:
    scope = ev.group_id or f"private:{ev.user_id}"
    return f"{ev.real_bot_id}:{scope}:{ev.user_id}"


def _on_cooldown(key: str) -> bool:
    now = time.monotonic()
    last = _last_reply.get(key)
    if last is not None and now - last < _COOLDOWN_SECONDS:
        return True
    if len(_last_reply) >= _MAX_COOLDOWN_KEYS:
        cutoff = now - _COOLDOWN_SECONDS
        for old_key in [k for k, ts in _last_reply.items() if ts < cutoff]:
            _last_reply.pop(old_key, None)
        while len(_last_reply) >= _MAX_COOLDOWN_KEYS:
            _last_reply.pop(next(iter(_last_reply)), None)
    _last_reply[key] = now
    return False


def _poke_prompt(ev: Event) -> str:
    where = f"群 {ev.group_id}" if ev.group_id else "私聊"
    return (
        f"（{where}里，用户 {ev.user_id} 刚刚戳了你一下。这不是一条文字消息，而是 QQ 的戳一戳动作。）\n"
        "请用你当前的人设，自然地回应这个戳一戳，一到两句话即可。\n"
        "不要解释系统机制、不要感谢或复述这段提示，直接说出要发给对方的话。"
    )


async def _ai_reply(bot: Bot, ev: Event) -> str:
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return ""

        from gsuid_core.ai_core.startup import is_ai_core_ready

        if not is_ai_core_ready():
            return ""

        from gsuid_core.ai_core.ai_router import get_ai_session
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.utils import (
            is_agent_error_text,
            is_silence_marker,
            strip_framework_user_leaks,
        )
    except Exception:
        logger.debug("[GsOnebotFunPlugin] poke AI module unavailable")
        return ""

    async with _ai_slots():
        try:
            session = await get_ai_session(ev)
            agent = create_agent(
                system_prompt=session.system_prompt,
                persona_name=session.persona_name,
                # 借用短调用档，保证最多只跑 5 轮模型请求
                create_by="ImageDescSummary",
                task_level="low",
                dynamic_tools=False,
                wall_clock_budget=0,
            )
            result = await asyncio.wait_for(
                agent.run(
                    _poke_prompt(ev),
                    bot=bot,
                    ev=ev,
                    tools=[],
                    return_mode="return",
                    budget_gate=True,
                ),
                timeout=_AI_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("[GsOnebotFunPlugin] poke AI reply timed out")
            return ""
        except Exception:
            logger.exception("[GsOnebotFunPlugin] poke AI reply failed")
            return ""

    text = strip_framework_user_leaks(str(result)).strip()
    if not text or is_silence_marker(text) or is_agent_error_text(text):
        return ""
    return text[:_MAX_REPLY_CHARS]


@sv.on_meta("poke")
async def poke_feedback(bot: Bot, ev: Event) -> None:
    target_id = str(ev.get_meta("target_id") or "")
    self_id = str(ev.bot_self_id or "")
    if target_id and self_id and target_id != self_id:
        return
    if not ev.user_id:
        return
    if _on_cooldown(_cooldown_key(ev)):
        return

    text = await _ai_reply(bot, ev)
    if not text:
        text = random.choice(_FALLBACKS)
    await bot.send(text)
