import asyncio

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("赞我", pm=6, area="ALL")

_TIMES_PER_CALL = 10  # send_like 单次最多 10 个，SVIP 日上限 50 要调用 5 次
_CALLS = 5
_CALL_GAP = 0.5


@sv.on_fullmatch(
    "赞我",
    to_ai="""
给当前用户的主号点赞

当用户说“赞我”“给我点赞”“帮我点个赞”时调用。
一次会尝试给用户点满 50 个赞，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["QQ 名片点赞", "给当前用户点赞", "一次最多 50 个赞"],
    aliases=["GsOnebotFunPlugin·赞我"],
)
async def zan_wo(bot: Bot, ev: Event) -> None:
    from plugins.OneBotUID.api import call_bot_api

    user_id = int(ev.user_id)
    for _ in range(_CALLS):
        await call_bot_api(
            "send_like",
            {"user_id": user_id, "times": _TIMES_PER_CALL},
            self_id=ev.bot_self_id,
        )
        await asyncio.sleep(_CALL_GAP)
    await bot.send(f"已赞 {_TIMES_PER_CALL * _CALLS} 次")
