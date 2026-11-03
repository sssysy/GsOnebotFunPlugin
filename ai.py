"""向 AI Core 注册插件知识，帮助自然语言召回本插件的功能。"""

from gsuid_core.ai_core.models import KnowledgePoint
from gsuid_core.ai_core.register import ai_alias, ai_entity

PLUGIN_NAME = "GsOnebotFunPlugin"

ai_entity(
    KnowledgePoint(
        id="gsonebotfunplugin_guide",
        plugin=PLUGIN_NAME,
        title="GsOnebotFunPlugin 功能说明",
        content="""
# GsOnebotFunPlugin

提供 QQ 点赞和群内早安晚安签到功能。

## 赞我
- 发送「赞我」给当前用户点满 50 个赞。

## 早安晚安
- 白天 06:00-17:59 发送「早」「早啊」「早安」「早上好」「醒了」「起床了」记录早起。
- 夜晚 18:00-次日 05:59 发送「晚」「晚安」「睡了」「我要睡了」「困了」记录睡觉。
- 同一会话同一人同一类型每天只记录一次。
- 睡眠时长只与前一晚的晚安记录配对；漏发晚安不会累计更早的记录。

## 排行榜
- 「早安排行」「早安榜」「早起排行」：按本群早安打卡次数排行。
- 「晚安排行」「晚安榜」「睡觉排行」：按本群晚安打卡次数排行。
- 「睡眠排行」「睡眠榜」：按本群平均睡眠时长排行。
- 「早晚安帮助」：查看签到与排行规则。
""",
        tags=["赞我", "早安", "晚安", "签到", "排行榜", "睡眠", "QQ"],
        entity="早安晚安签到",
    )
)

ai_alias("早安晚安签到", ["早晚安", "早安晚安", "早起打卡", "睡觉打卡"], scope=PLUGIN_NAME)
