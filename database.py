"""GsOnebotFunPlugin 数据表：早安 / 晚安签到与 Iwara 博主巡检状态。"""

import json
import asyncio

from sqlmodel import Field, col, func, delete, select
from sqlalchemy import CursorResult, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.server import on_core_start_before
from gsuid_core.utils.database.base_models import (
    BaseModel,
    BaseIDModel,
    with_session,
    with_read_session,
)

RECORD_EARLY = "early"
RECORD_NIGHT = "night"

_TABLE_NAME = "zaoanwananrecord"
_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()

_IWARA_TABLE_READY = False
_IWARA_TABLE_LOCK = asyncio.Lock()


class ZaoanWananRecord(BaseModel, table=True):
    """群成员每日早起 / 晚睡签到记录。"""

    __table_args__ = {"extend_existing": True}

    group_id: str = Field(default="", title="群号")
    record_type: str = Field(default=RECORD_EARLY, title="类型 early=早起 night=晚睡")
    sign_day: str = Field(default="", title="归属自然日 YYYY-MM-DD")
    sign_ts: int = Field(default=0, title="签到时间戳(秒)")
    seq: int = Field(default=0, title="当日序号")
    nickname: str = Field(default="", max_length=64, title="签到时的昵称")
    sleep_seconds: int = Field(default=0, title="本次睡眠时长(秒)")
    activity_seconds: int = Field(default=0, title="本次清醒时长(秒)")

    @classmethod
    async def _ensure_ready(cls, session: AsyncSession) -> None:
        global _TABLE_READY
        if _TABLE_READY:
            return
        async with _TABLE_LOCK:
            if _TABLE_READY:
                return
            ddl = str(CreateTable(cls.__table__, if_not_exists=True))
            await session.execute(text(ddl))
            existing = set((await session.execute(text(f"SELECT * FROM {_TABLE_NAME} LIMIT 1"))).keys())
            extra_columns = (
                ("nickname", "VARCHAR(64) DEFAULT ''"),
                ("sleep_seconds", "INTEGER DEFAULT 0"),
                ("activity_seconds", "INTEGER DEFAULT 0"),
            )
            for name, column_type in extra_columns:
                if name not in existing:
                    await session.execute(text(f"ALTER TABLE {_TABLE_NAME} ADD COLUMN {name} {column_type}"))
            _TABLE_READY = True

    @classmethod
    @with_session
    async def ensure_table(cls, session: AsyncSession) -> None:
        await cls._ensure_ready(session)

    @classmethod
    @with_session
    async def sign(
        cls,
        session: AsyncSession,
        group_id: str,
        user_id: str,
        nickname: str,
        record_type: str,
        sign_day: str,
        sign_ts: int,
        bot_id: str = "",
        sleep_seconds: int = 0,
        activity_seconds: int = 0,
    ) -> tuple[bool, int]:
        """签到；同一群 + 同一人 + 同类型 + 同一归属日只算一次。"""
        await cls._ensure_ready(session)
        nickname = nickname[:64]
        exist_stmt = (
            select(cls)
            .where(cls.group_id == group_id)
            .where(cls.user_id == user_id)
            .where(cls.record_type == record_type)
            .where(cls.sign_day == sign_day)
        )
        exist_res = await session.execute(exist_stmt)
        exist = exist_res.scalars().first()
        if exist is not None:
            if nickname and exist.nickname != nickname:
                exist.nickname = nickname
                session.add(exist)
            return False, int(exist.seq)

        count_stmt = (
            select(func.count())
            .select_from(cls)
            .where(cls.group_id == group_id)
            .where(cls.record_type == record_type)
            .where(cls.sign_day == sign_day)
        )
        count_res = await session.execute(count_stmt)
        raw_count = count_res.scalar_one_or_none()
        count = raw_count if isinstance(raw_count, int) else 0

        session.add(
            cls(
                group_id=group_id,
                user_id=user_id,
                bot_id=bot_id,
                record_type=record_type,
                sign_day=sign_day,
                sign_ts=sign_ts,
                seq=count + 1,
                nickname=nickname,
                sleep_seconds=sleep_seconds,
                activity_seconds=activity_seconds,
            )
        )
        return True, count + 1

    @classmethod
    @with_read_session
    async def get_by_day(
        cls,
        session: AsyncSession,
        group_id: str,
        user_id: str,
        record_type: str,
        sign_day: str,
    ) -> "ZaoanWananRecord | None":
        """取某用户在指定归属日的唯一一条记录。"""
        stmt = (
            select(cls)
            .where(cls.group_id == group_id)
            .where(cls.user_id == user_id)
            .where(cls.record_type == record_type)
            .where(cls.sign_day == sign_day)
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    @classmethod
    @with_read_session
    async def get_scope_records(
        cls,
        session: AsyncSession,
        group_id: str,
        record_type: str,
    ) -> "list[ZaoanWananRecord]":
        """取某会话某类型的全部记录，按时间升序供榜单统计。"""
        stmt = (
            select(cls)
            .where(cls.group_id == group_id)
            .where(cls.record_type == record_type)
            .order_by(col(cls.sign_ts).asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @classmethod
    @with_read_session
    async def get_scope_all_records(
        cls,
        session: AsyncSession,
        group_id: str,
    ) -> "list[ZaoanWananRecord]":
        """取某会话全部记录，按时间升序；榜单需要把早晚安按时间配对。"""
        stmt = select(cls).where(cls.group_id == group_id).order_by(col(cls.sign_ts).asc(), col(cls.id).asc())
        res = await session.execute(stmt)
        return list(res.scalars().all())


def parse_known_id_set(raw: str) -> set[str]:
    if not raw:
        return set()
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {item for item in parsed if isinstance(item, str) and item}


def dump_known_id_set(ids: set[str]) -> str:
    return json.dumps(sorted(ids), ensure_ascii=False)


class IwaraBloggerState(BaseIDModel, table=True):
    """Iwara 被订阅博主的巡检基线与已知作品 ID 记录。"""

    __table_args__ = {"extend_existing": True}

    username: str = Field(default="", index=True, title="Iwara 用户名")
    iwara_user_id: str = Field(default="", title="Iwara 用户 UUID")
    known_video_ids: str = Field(default="[]", title="已知视频 ID 列表(JSON)")
    known_image_ids: str = Field(default="[]", title="已知图片 ID 列表(JSON)")
    polled: bool = Field(default=False, title="是否已完成首次巡检")

    @classmethod
    async def _ensure_ready(cls, session: AsyncSession) -> None:
        global _IWARA_TABLE_READY
        if _IWARA_TABLE_READY:
            return
        async with _IWARA_TABLE_LOCK:
            if _IWARA_TABLE_READY:
                return
            ddl = str(CreateTable(cls.__table__, if_not_exists=True))
            await session.execute(text(ddl))
            _IWARA_TABLE_READY = True

    @classmethod
    @with_session
    async def ensure_table(cls, session: AsyncSession) -> None:
        await cls._ensure_ready(session)

    @classmethod
    @with_session
    async def upsert_blogger(
        cls,
        session: AsyncSession,
        username: str,
        iwara_user_id: str,
    ) -> "IwaraBloggerState":
        await cls._ensure_ready(session)
        stmt = select(cls).where(col(cls.username) == username).limit(1)
        res = await session.execute(stmt)
        row = res.scalars().first()
        if row is not None:
            if iwara_user_id and row.iwara_user_id != iwara_user_id:
                row.iwara_user_id = iwara_user_id
                session.add(row)
            return row
        created = cls(
            username=username,
            iwara_user_id=iwara_user_id,
            known_video_ids="[]",
            known_image_ids="[]",
            polled=False,
        )
        session.add(created)
        return created

    @classmethod
    @with_read_session
    async def get_by_username(
        cls,
        session: AsyncSession,
        username: str,
    ) -> "IwaraBloggerState | None":
        stmt = select(cls).where(col(cls.username) == username).limit(1)
        res = await session.execute(stmt)
        return res.scalars().first()

    @classmethod
    @with_session
    async def update_known_ids(
        cls,
        session: AsyncSession,
        username: str,
        video_ids: set[str],
        image_ids: set[str],
        polled: bool,
    ) -> None:
        await cls._ensure_ready(session)
        stmt = select(cls).where(col(cls.username) == username).limit(1)
        res = await session.execute(stmt)
        row = res.scalars().first()
        if row is None:
            return
        row.known_video_ids = dump_known_id_set(video_ids)
        row.known_image_ids = dump_known_id_set(image_ids)
        row.polled = polled
        session.add(row)

    @classmethod
    @with_session
    async def delete_by_username(cls, session: AsyncSession, username: str) -> int:
        await cls._ensure_ready(session)
        stmt = delete(cls).where(col(cls.username) == username)
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0


@on_core_start_before
async def _ensure_zaoan_wanan_table() -> None:
    await ZaoanWananRecord.ensure_table()
    await IwaraBloggerState.ensure_table()


async def ensure_zaoan_wanan_table() -> None:
    """热加载后首次写入前的兜底建表入口。"""
    if _TABLE_READY:
        return
    await ZaoanWananRecord.ensure_table()


async def ensure_iwara_state_table() -> None:
    """热加载后首次访问 Iwara 状态表前的兜底建表入口。"""
    if _IWARA_TABLE_READY:
        return
    await IwaraBloggerState.ensure_table()
