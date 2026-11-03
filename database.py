"""GsOnebotFunPlugin 数据表：早安 / 晚安签到。"""

import asyncio

from sqlmodel import Field, col, func, select
from sqlalchemy import text
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.server import on_core_start_before
from gsuid_core.utils.database.base_models import (
    BaseModel,
    with_session,
    with_read_session,
)

RECORD_EARLY = "early"
RECORD_NIGHT = "night"

_TABLE_NAME = "zaoanwananrecord"
_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()


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


@on_core_start_before
async def _ensure_zaoan_wanan_table() -> None:
    await ZaoanWananRecord.ensure_table()


async def ensure_zaoan_wanan_table() -> None:
    """热加载后首次写入前的兜底建表入口。"""
    if _TABLE_READY:
        return
    await ZaoanWananRecord.ensure_table()
