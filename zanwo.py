import asyncio
import random

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("赞我", pm=6, area="ALL")

_TIMES_PER_CALL = 10  # send_like 单次最多 10 个，SVIP 日上限 50 要调用 5 次
_CALLS = 5
_CALL_GAP = 0.5
_SUCCESS_MESSAGES = (
    "赞完啦～今天的你也闪闪发光✨",
    "点赞小分队出动完毕，请查收这份心意～",
    "咔咔咔，赞已经送到，记得开心呀！",
    "任务完成！愿好运和点赞一起奔向你～",
    "赞已经安排上啦，今天也要元气满满哦！",
    "叮咚～点赞已送达，请签收这份小小的心意！",
)


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
    from plugins.OneBotUID.api import OneBotApiError, call_bot_api

    user_id = int(ev.user_id)
    for index in range(_CALLS):
        try:
            await call_bot_api(
                "send_like",
                {"user_id": user_id, "times": _TIMES_PER_CALL},
                self_id=ev.bot_self_id,
            )
        except OneBotApiError:
            if index == 0:
                raise
            break
        if index < _CALLS - 1:
            await asyncio.sleep(_CALL_GAP)
    await bot.send(random.choice(_SUCCESS_MESSAGES))
