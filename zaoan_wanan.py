"""早安 / 晚安签到与排行榜。"""

from datetime import datetime, timedelta
from dataclasses import dataclass

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .database import (
    RECORD_EARLY,
    RECORD_NIGHT,
    ZaoanWananRecord,
    ensure_zaoan_wanan_table,
)

sv = SV("早安晚安签到", aside=False)

DAY_START_HOUR = 6
DAY_END_HOUR = 18
# 睡眠 / 清醒最多认一天：漏签时宁可不出时长，也不能把几十小时算进睡眠
MAX_PAIR_SECONDS = 24 * 3600
RANK_LIMIT = 10

_TAIL = r"[\s呀啊啦了哦喔呢嘛呐鸭哇喽咯~～!！。,.，?？]*"
MORNING_PATTERN = r"^\s*(?:早(?:安|早)*|早晨|早上好|醒(?:了|啦)|起床(?:了|啦)?|我醒了|我起了|我起床了)" + _TAIL + r"$"
NIGHT_PATTERN = r"^\s*(?:晚(?:安|晚)*|[我该得去要]*睡(?:觉)?(?:了|啦)|困了)" + _TAIL + r"$"


@dataclass
class CountEntry:
    user_id: str
    nickname: str
    count: int = 0


@dataclass
class SleepEntry:
    user_id: str
    nickname: str
    total_seconds: int = 0
    nights: int = 0

    @property
    def average_seconds(self) -> int:
        return self.total_seconds // self.nights if self.nights else 0


def _now() -> datetime:
    return datetime.now()


def _is_day(now: datetime) -> bool:
    return DAY_START_HOUR <= now.hour < DAY_END_HOUR


def _night_anchor_day(now: datetime) -> str:
    """18:00 后算当天，00:00-05:59 算前一天，让一晚只落一天。"""
    if now.hour < DAY_START_HOUR:
        return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def _scope_id(ev: Event) -> str:
    if ev.group_id:
        return str(ev.group_id)
    return f"private:{ev.user_id}"


def _nickname(ev: Event) -> str:
    if "nickname" not in ev.sender:
        return ""
    raw: object = ev.sender["nickname"]
    if isinstance(raw, str):
        name = " ".join(raw.split())
        return name[:64]
    return ""


def _display_name(record: ZaoanWananRecord) -> str:
    return record.nickname or record.user_id


def _fmt_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    return f"{hours} 小时 {minutes} 分"


def _paired_seconds(start_ts: int, end_ts: int) -> int:
    diff = end_ts - start_ts
    if diff < 0 or diff > MAX_PAIR_SECONDS:
        return 0
    return diff


def _count_rank(records: list[ZaoanWananRecord]) -> list[CountEntry]:
    stats: dict[str, CountEntry] = {}
    for record in records:
        if record.user_id in stats:
            entry = stats[record.user_id]
            entry.count += 1
            if record.nickname:
                entry.nickname = record.nickname
        else:
            stats[record.user_id] = CountEntry(
                user_id=record.user_id,
                nickname=_display_name(record),
                count=1,
            )
    return sorted(stats.values(), key=lambda item: (-item.count, item.user_id))


def _sleep_rank(
    records: list[ZaoanWananRecord],
) -> list[SleepEntry]:
    """一晚配对一次早签；漏发晚安直接跳过，绝不复用更早的记录。"""
    pending: dict[str, int] = {}
    paired: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    for record in records:
        if record.nickname:
            names[record.user_id] = record.nickname
        if record.record_type == RECORD_NIGHT:
            pending[record.user_id] = record.sign_ts
            continue
        if record.user_id not in pending:
            continue
        night_ts = pending[record.user_id]
        del pending[record.user_id]
        stored = record.sleep_seconds
        seconds = stored if 0 < stored <= MAX_PAIR_SECONDS else 0
        if seconds <= 0:
            seconds = _paired_seconds(night_ts, record.sign_ts)
        if seconds <= 0:
            continue
        if record.user_id in paired:
            paired[record.user_id].append(seconds)
        else:
            paired[record.user_id] = [seconds]

    stats: dict[str, SleepEntry] = {}
    for user_id, seconds_list in paired.items():
        nickname = user_id
        if user_id in names:
            nickname = names[user_id]
        stats[user_id] = SleepEntry(
            user_id=user_id,
            nickname=nickname,
            total_seconds=sum(seconds_list),
            nights=len(seconds_list),
        )
    return sorted(
        stats.values(),
        key=lambda item: (-item.average_seconds, item.user_id),
    )


def _rank_lines(title: str, rows: list[str]) -> list[str]:
    if not rows:
        return [f"【{title}】", "还没有记录～"]
    return [f"【{title}】", *rows]


@sv.on_regex(
    MORNING_PATTERN,
    to_ai="""
记录今天的早起签到

当用户说“早安”“早上好”“醒了”“起床了”并想记录起床时调用。
仅在 06:00-17:59 有效；同一会话同一人每天只记录一次。

Args:
    text: 一句匹配早起说法的消息，例如“早安”“早上好”“我醒了”
""",
    covers=["早安签到", "起床记录", "早起打卡", "睡眠时长"],
    aliases=["GsOnebotFunPlugin·早安签到"],
)
async def zaoan_good_morning(bot: Bot, ev: Event) -> None:
    now = _now()
    if not _is_day(now):
        await bot.send("现在是晚上啦～发「晚安」「睡了」记录睡觉吧！")
        return

    await ensure_zaoan_wanan_table()
    scope_id = _scope_id(ev)
    user_id = str(ev.user_id)
    day = now.strftime("%Y-%m-%d")
    night_day = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    now_ts = int(now.timestamp())

    last_night = await ZaoanWananRecord.get_by_day(scope_id, user_id, RECORD_NIGHT, night_day)
    sleep_seconds = _paired_seconds(last_night.sign_ts, now_ts) if last_night is not None else 0

    created, seq = await ZaoanWananRecord.sign(
        scope_id,
        user_id,
        _nickname(ev),
        RECORD_EARLY,
        day,
        now_ts,
        str(ev.bot_id),
        sleep_seconds=sleep_seconds,
    )
    if not created:
        await bot.send(f"你今天已经签到过了，是今天第 {seq} 个早起的～")
        return

    lines = [f"早安！你是今天第 {seq} 个早起的"]
    if sleep_seconds > 0:
        lines.append(f"昨晚睡了 {_fmt_duration(sleep_seconds)}")
    await bot.send("\n".join(lines))


@sv.on_regex(
    NIGHT_PATTERN,
    to_ai="""
记录今晚的睡觉签到

当用户说“晚安”“睡了”“我要睡了”“困了”并想记录睡觉时调用。
仅在 18:00-次日 05:59 有效；同一会话同一人每晚只记录一次。

Args:
    text: 一句匹配睡觉说法的消息，例如“晚安”“睡了”“我要睡了”
""",
    covers=["晚安签到", "睡觉记录", "晚睡打卡", "睡眠时长"],
    aliases=["GsOnebotFunPlugin·晚安签到"],
)
async def zaoan_good_night(bot: Bot, ev: Event) -> None:
    now = _now()
    if _is_day(now):
        await bot.send("现在是白天哦～发「早」「早安」记录起床吧！")
        return

    await ensure_zaoan_wanan_table()
    scope_id = _scope_id(ev)
    user_id = str(ev.user_id)
    night_day = _night_anchor_day(now)
    now_ts = int(now.timestamp())

    last_early = await ZaoanWananRecord.get_by_day(scope_id, user_id, RECORD_EARLY, night_day)
    activity_seconds = _paired_seconds(last_early.sign_ts, now_ts) if last_early is not None else 0

    created, seq = await ZaoanWananRecord.sign(
        scope_id,
        user_id,
        _nickname(ev),
        RECORD_NIGHT,
        night_day,
        now_ts,
        str(ev.bot_id),
        activity_seconds=activity_seconds,
    )
    if not created:
        await bot.send(f"你今晚已经记过睡觉啦，是本群第 {seq} 个睡觉的～")
        return

    lines = [f"晚安！你是本群第 {seq} 个睡觉的"]
    if activity_seconds > 0:
        lines.append(f"今天醒着 {_fmt_duration(activity_seconds)}")
    await bot.send("\n".join(lines))


@sv.on_fullmatch(
    ("早安排行", "早安榜", "早起排行"),
    to_ai="""
查看当前会话的早安打卡排行

当用户问“早安排行”“早安榜”“谁起得最早”“早起排行”时调用。
按本群早安签到次数降序展示前 10 名，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["群内早安打卡次数排行", "早起排行榜"],
    aliases=["GsOnebotFunPlugin·早安排行"],
)
async def zaoan_rank_early(bot: Bot, ev: Event) -> None:
    await ensure_zaoan_wanan_table()
    records = await ZaoanWananRecord.get_scope_records(_scope_id(ev), RECORD_EARLY)
    rank = _count_rank(records)[:RANK_LIMIT]
    rows = [f"{index}. {item.nickname} {item.count} 次" for index, item in enumerate(rank, 1)]
    lines = _rank_lines("早安排行", rows)
    if rank:
        total = len({record.user_id for record in records})
        lines.append(f"共 {total} 人参与")
    await bot.send("\n".join(lines))


@sv.on_fullmatch(
    ("晚安排行", "晚安榜", "睡觉排行"),
    to_ai="""
查看当前会话的晚安打卡排行

当用户问“晚安排行”“晚安榜”“谁睡得最早”“睡觉排行”时调用。
按本群晚安签到次数降序展示前 10 名，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["群内晚安打卡次数排行", "睡觉次数排行榜"],
    aliases=["GsOnebotFunPlugin·晚安排行"],
)
async def zaoan_rank_night(bot: Bot, ev: Event) -> None:
    await ensure_zaoan_wanan_table()
    records = await ZaoanWananRecord.get_scope_records(_scope_id(ev), RECORD_NIGHT)
    rank = _count_rank(records)[:RANK_LIMIT]
    rows = [f"{index}. {item.nickname} {item.count} 次" for index, item in enumerate(rank, 1)]
    lines = _rank_lines("晚安排行", rows)
    if rank:
        total = len({record.user_id for record in records})
        lines.append(f"共 {total} 人参与")
    await bot.send("\n".join(lines))


@sv.on_fullmatch(
    ("睡眠排行", "睡眠榜"),
    to_ai="""
查看当前会话的平均睡眠时长排行

当用户问“睡眠排行”“睡眠榜”“谁睡得最久”“平均睡多久”时调用。
按本群平均睡眠时长降序展示前 10 名，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["群内平均睡眠时长排行", "睡眠时长统计"],
    aliases=["GsOnebotFunPlugin·睡眠排行"],
)
async def zaoan_rank_sleep(bot: Bot, ev: Event) -> None:
    await ensure_zaoan_wanan_table()
    records = await ZaoanWananRecord.get_scope_all_records(_scope_id(ev))
    all_rank = _sleep_rank(records)
    rank = all_rank[:RANK_LIMIT]
    rows = [
        f"{index}. {item.nickname} 平均 {_fmt_duration(item.average_seconds)}（{item.nights} 晚）"
        for index, item in enumerate(rank, 1)
    ]
    lines = _rank_lines("睡眠排行", rows)
    if rank:
        lines.append(f"共 {len(all_rank)} 人有睡眠记录")
    await bot.send("\n".join(lines))


@sv.on_fullmatch(
    ("早晚安帮助", "早安帮助", "晚安帮助"),
    to_ai="""
查看早安晚安签到功能说明

当用户询问早晚安怎么签到、哪些词能触发、榜单怎么算时调用。
返回签到时间段、可用说法、排行榜与统计规则，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["早安晚安签到玩法说明", "早晚安命令帮助", "睡眠统计规则"],
    aliases=["GsOnebotFunPlugin·早晚安帮助"],
)
async def zaoan_help(bot: Bot, ev: Event) -> None:
    text = (
        "【早安晚安签到】\n"
        "白天 6:00-17:59：早 / 早啊 / 早安 / 早上好 / 醒了 / 起床了\n"
        "夜晚 18:00-次日 5:59：晚 / 晚安 / 睡了 / 睡啦 / 我要睡了 / 困了\n"
        "早安排行：早安打卡次数排行\n"
        "晚安排行：晚安打卡次数排行\n"
        "睡眠排行：平均睡眠时长排行\n"
        "同一人同一天只算一次；没有前一晚的晚安记录时不计算睡眠时长。"
    )
    await bot.send(text)
