"""Iwara 博主更新订阅命令、定时巡检与纯文字推送。"""

from __future__ import annotations

import asyncio

from gsuid_core.sv import SV
from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.server import on_core_start
from gsuid_core.segment import MessageSegment
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.utils.database.models import Subscribe

from .config import (
    DEFAULT_SITE_HOST,
    get_iwara_str,
    get_iwara_poll_interval_min,
    register_poll_interval_listener,
)
from .database import (
    IwaraBloggerState,
    parse_known_id_set,
    ensure_iwara_state_table,
)
from .iwara_client import IwaraApiError, IwaraMediaItem, iwara_client

IWARA_TASK_NAME = "GsOnebotFunPlugin_iwara"
IWARA_POLL_JOB_ID = "gs_onebot_fun_iwara_poll"

_AT_OFF = "off"
_AT_ME = "atme"
_AT_ALL = "atall"
_AT_LABELS: dict[str, str] = {
    _AT_ME: "(@订阅者)",
    _AT_ALL: "(@全体成员)",
}
_POLL_LOCK = asyncio.Lock()
_BLOGGER_GAP_SEC = 1.0
_GROUP_ADMIN_MAX_PM = 3

sv = SV("iwara订阅", pm=6, area="ALL")


def _site_host() -> str:
    raw = get_iwara_str("iwara_site_host", DEFAULT_SITE_HOST)
    host = raw.removeprefix("https://").removeprefix("http://").strip("/")
    return host or DEFAULT_SITE_HOST


def _same_session(sub: Subscribe, ev: Event) -> bool:
    if sub.bot_id != ev.bot_id:
        return False
    if ev.user_type == "group":
        return sub.user_type == "group" and sub.group_id == ev.group_id
    return sub.user_type == ev.user_type and sub.user_id == ev.user_id


async def _get_session_subscriptions(ev: Event) -> list[Subscribe]:
    rows = await gs_subscribe.get_subscribe(IWARA_TASK_NAME)
    if not rows:
        return []
    return [row for row in rows if row.uid and _same_session(row, ev)]


def _parse_subscribe_args(raw_text: str) -> tuple[str, str] | str:
    parts = raw_text.strip().split()
    if not parts:
        return "用法：iwara订阅<用户名> [atme|atall]\n例如：iwara订阅 nightmate71 atme"
    username = parts[0].lstrip("@").strip()
    if not username:
        return "用户名不能为空。\n用法：iwara订阅<用户名> [atme|atall]"
    if len(parts) == 1:
        return username, _AT_OFF
    selector = parts[1].strip().lower()
    if selector in (_AT_ME, _AT_ALL):
        return username, selector
    return "无效的选择器，可选：atme（@订阅者）| atall（@全体成员），留空则不@。"


def _format_subscribe_reply(username: str, at_mode: str, ev: Event) -> str:
    if at_mode == _AT_ME:
        base = f"✅ 已订阅 {username}！有新视频/图片时会 @你 通知。"
    elif at_mode == _AT_ALL:
        base = f"✅ 已订阅 {username}！有新视频/图片时会 @全体成员 通知。"
    else:
        base = f"✅ 已订阅 {username}！有新视频/图片时会发送更新通知。"

    hints: list[str] = []
    if at_mode != _AT_OFF and ev.user_type != "group":
        hints.append("提示：当前为私聊会话，推送时会自动跳过 @。")
    elif at_mode == _AT_ME and not ev.user_id:
        hints.append("⚠️ 当前平台未提供你的用户 ID，@可能不生效。")
    if hints:
        return base + "\n" + "\n".join(hints)
    return base


@sv.on_fullmatch(
    "iwara订阅列表",
    block=True,
    to_ai="""
查看当前会话已订阅的 Iwara 博主列表

当用户询问当前群或私聊订阅了哪些 Iwara 博主时调用，无需额外参数。

Args:
    text: 无需参数，留空即可
""",
    covers=["查看 Iwara 订阅列表", "列出已订阅的 Iwara 博主"],
    aliases=["GsOnebotFunPlugin·iwara订阅列表"],
)
async def iwara_sub_list(bot: Bot, ev: Event) -> None:
    subs = await _get_session_subscriptions(ev)
    if not subs:
        await bot.send("当前会话还没有订阅任何 Iwara 博主。\n使用「iwara订阅<用户名> [atme|atall]」添加订阅。")
        return
    subs_sorted = sorted(subs, key=lambda item: (item.uid or "").lower())
    lines: list[str] = ["当前会话的 Iwara 订阅列表："]
    for index, sub in enumerate(subs_sorted, start=1):
        name = sub.uid or ""
        mode = (sub.extra_message or _AT_OFF).strip().lower()
        label = _AT_LABELS[mode] if mode in _AT_LABELS else ""
        suffix = f" {label}" if label else ""
        lines.append(f"[{index}] {name}{suffix}")
    await bot.send("\n".join(lines))


@sv.on_command(
    "iwara订阅",
    block=True,
    to_ai="""
订阅指定 Iwara 博主的视频与图片更新推送

当用户要求订阅某个 Iwara 博主、作者或更新推送时调用。
重复订阅同一博主会更新提醒模式，不传选择器会重置为不 @。

Args:
    text: 博主用户名与可选选择器，格式为「<用户名> [atme|atall]」
""",
    covers=["订阅 Iwara 博主更新", "设置 Iwara 新视频与新图片推送提醒"],
    aliases=["GsOnebotFunPlugin·iwara订阅"],
)
async def iwara_sub(bot: Bot, ev: Event) -> None:
    raw_text = ev.text.strip()
    if raw_text == "列表":
        await iwara_sub_list(bot, ev)
        return

    parsed = _parse_subscribe_args(raw_text)
    if isinstance(parsed, str):
        await bot.send(parsed)
        return
    username, at_mode = parsed

    if at_mode == _AT_ALL and ev.user_type == "group" and ev.user_pm > _GROUP_ADMIN_MAX_PM:
        await bot.send("❌ 仅群管理员或主人可设置 atall（@全体成员）模式。")
        return

    try:
        profile = await iwara_client.get_user_profile(username)
    except IwaraApiError as exc:
        await bot.send(f"查询 Iwara 用户失败：{exc}")
        return

    if profile is None:
        await bot.send(f"未找到 Iwara 用户 {username}。")
        return

    await ensure_iwara_state_table()
    await IwaraBloggerState.upsert_blogger(profile.username, profile.user_id)
    await gs_subscribe.add_subscribe(
        subscribe_type="session",
        task_name=IWARA_TASK_NAME,
        event=ev,
        extra_message=at_mode,
        uid=profile.username,
        extra_data=ev.user_id,
    )
    await bot.send(_format_subscribe_reply(profile.username, at_mode, ev))


@sv.on_command(
    "iwara取消订阅",
    block=True,
    to_ai="""
取消当前会话对指定 Iwara 博主的更新订阅

当用户要求取消订阅或停止推送某个 Iwara 博主更新时调用。

Args:
    text: 要取消订阅的 Iwara 博主用户名
""",
    covers=["取消订阅 Iwara 博主", "停止 Iwara 更新推送"],
    aliases=["GsOnebotFunPlugin·iwara取消订阅"],
)
async def iwara_unsub(bot: Bot, ev: Event) -> None:
    username = ev.text.strip().lstrip("@").strip()
    if not username:
        await bot.send("用法：iwara取消订阅<用户名>")
        return

    session_subs = await _get_session_subscriptions(ev)
    target_lower = username.lower()
    matched_uid = ""
    for sub in session_subs:
        if sub.uid and sub.uid.lower() == target_lower:
            matched_uid = sub.uid
            break

    if not matched_uid:
        await bot.send(f"当前会话未订阅 {username}。")
        return

    await gs_subscribe.delete_subscribe(
        "session",
        IWARA_TASK_NAME,
        ev,
        uid=matched_uid,
    )

    remaining = await gs_subscribe.get_subscribe(IWARA_TASK_NAME, uid=matched_uid)
    if not remaining:
        await ensure_iwara_state_table()
        await IwaraBloggerState.delete_by_username(matched_uid)

    await bot.send(f"已取消订阅 {matched_uid}。")


async def _poll_single_blogger(
    username: str,
) -> tuple[list[IwaraMediaItem], list[IwaraMediaItem]]:
    await ensure_iwara_state_table()
    state = await IwaraBloggerState.get_by_username(username)
    if state is None or not state.iwara_user_id:
        profile = await iwara_client.get_user_profile(username)
        if profile is None:
            logger.warning(f"[GsOnebotFunPlugin] Iwara 巡检未找到用户 {username}")
            return [], []
        state = await IwaraBloggerState.upsert_blogger(username, profile.user_id)

    known_videos = parse_known_id_set(state.known_video_ids)
    known_images = parse_known_id_set(state.known_image_ids)
    first_poll = not state.polled and not known_videos and not known_images

    new_videos: list[IwaraMediaItem] = []
    new_images: list[IwaraMediaItem] = []
    videos_ok = False
    images_ok = False

    try:
        videos = await iwara_client.get_user_videos(state.iwara_user_id, limit=10)
        videos_ok = True
        for item in videos:
            if item.item_id not in known_videos and not first_poll:
                new_videos.append(item)
            known_videos.add(item.item_id)
    except IwaraApiError as exc:
        logger.warning(f"[GsOnebotFunPlugin] 拉取 {username} 视频列表失败: {exc}")

    try:
        images = await iwara_client.get_user_images(state.iwara_user_id, limit=10)
        images_ok = True
        for item in images:
            if item.item_id not in known_images and not first_poll:
                new_images.append(item)
            known_images.add(item.item_id)
    except IwaraApiError as exc:
        logger.warning(f"[GsOnebotFunPlugin] 拉取 {username} 图片列表失败: {exc}")

    # 首次巡检至少一类接口成功才标记基线完成，避免首次断网导致下次全量刷屏
    next_polled = state.polled or videos_ok or images_ok
    await IwaraBloggerState.update_known_ids(
        username,
        video_ids=known_videos,
        image_ids=known_images,
        polled=next_polled,
    )
    return list(reversed(new_videos)), list(reversed(new_images))


async def _notify_subscriber(sub: Subscribe, text: str) -> None:
    is_group = sub.user_type == "group"
    at_mode = (sub.extra_message or _AT_OFF).strip().lower()
    sender_id = (sub.extra_data or sub.user_id or "").strip()

    at_segment: Message | None = None
    if is_group and at_mode == _AT_ME and sender_id:
        at_segment = MessageSegment.at(sender_id)
    elif is_group and at_mode == _AT_ALL:
        at_segment = MessageSegment.at("all")

    if at_segment is not None:
        try:
            await sub.send([at_segment, MessageSegment.text("\n" + text)])
            return
        except Exception as exc:
            logger.warning(f"[GsOnebotFunPlugin] Iwara 带@推送失败，降级为纯文本 (group={sub.group_id}): {exc}")

    try:
        await sub.send(text)
    except Exception as exc:
        logger.warning(
            "[GsOnebotFunPlugin] Iwara 推送失败 "
            f"(type={sub.user_type}, group={sub.group_id}, user={sub.user_id}): {exc}"
        )


async def poll_all_iwara_subscriptions() -> None:
    """定时巡检所有已订阅的 Iwara 博主，并向订阅会话推送纯文本通知。"""
    if _POLL_LOCK.locked():
        logger.debug("[GsOnebotFunPlugin] 上一轮 Iwara 订阅巡检尚未结束，跳过本轮")
        return

    async with _POLL_LOCK:
        # APScheduler 会吞掉任务异常，这里统一兜底记录日志
        try:
            all_subs = await gs_subscribe.get_subscribe(IWARA_TASK_NAME)
            if not all_subs:
                return

            grouped: dict[str, list[Subscribe]] = {}
            for sub in all_subs:
                if not sub.uid:
                    continue
                if sub.uid in grouped:
                    grouped[sub.uid].append(sub)
                else:
                    grouped[sub.uid] = [sub]

            host = _site_host()
            bloggers = list(grouped.items())
            for idx, (username, subscribers) in enumerate(bloggers):
                try:
                    new_videos, new_images = await _poll_single_blogger(username)
                except Exception as exc:
                    logger.warning(f"[GsOnebotFunPlugin] 巡检 Iwara 博主 {username} 异常: {exc}")
                    continue

                for item in new_videos:
                    text = (
                        f"🔔 订阅通知: {username} 发布了新视频!\n"
                        f"标题: {item.title}\n"
                        f"https://{host}/video/{item.item_id}"
                    )
                    for sub in subscribers:
                        await _notify_subscriber(sub, text)

                for item in new_images:
                    text = (
                        f"🔔 订阅通知: {username} 发布了新图片!\n"
                        f"标题: {item.title}\n"
                        f"https://{host}/image/{item.item_id}"
                    )
                    for sub in subscribers:
                        await _notify_subscriber(sub, text)

                if idx < len(bloggers) - 1:
                    await asyncio.sleep(_BLOGGER_GAP_SEC)
        except Exception:
            logger.exception("[GsOnebotFunPlugin] Iwara 订阅定时巡检发生未预期异常")


def schedule_iwara_poll(interval_min: int | None = None) -> None:
    """按最新配置的分钟数注册或热更新 APScheduler 轮询任务。"""
    minutes = interval_min if interval_min is not None else get_iwara_poll_interval_min()
    if scheduler.get_job(IWARA_POLL_JOB_ID) is not None:
        scheduler.remove_job(IWARA_POLL_JOB_ID)
    scheduler.add_job(
        poll_all_iwara_subscriptions,
        trigger="interval",
        minutes=minutes,
        id=IWARA_POLL_JOB_ID,
        name="Iwara 订阅轮询",
        replace_existing=True,
    )
    logger.info(f"[GsOnebotFunPlugin] 已更新 Iwara 订阅轮询任务，间隔 {minutes} 分钟")


register_poll_interval_listener(schedule_iwara_poll)


@on_core_start
async def _start_iwara_poll_scheduler() -> None:
    schedule_iwara_poll()
