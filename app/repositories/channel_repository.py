from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models.channel import MonitoredChannel


class ChannelRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_monitored_channel(self, channel_id: int, channel_username: str, account_id: int):
        new_channel = MonitoredChannel(
            channel_id=channel_id, channel_username=channel_username, account_id=account_id, last_processed_msg_id=0
        )
        self.session.add(new_channel)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()  # قبلاً ثبت شده
            return None
        return new_channel

    async def get_monitored_channels(self, account_id: int):
        stmt = select(MonitoredChannel).where(MonitoredChannel.account_id == account_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update_last_processed_msg(self, channel_id: int, account_id: int, msg_id: int):
        # فقط به جلو حرکت می‌کند
        stmt = update(MonitoredChannel).where(
            MonitoredChannel.channel_id == channel_id,
            MonitoredChannel.account_id == account_id,
            MonitoredChannel.last_processed_msg_id < msg_id,
        ).values(last_processed_msg_id=msg_id)
        await self.session.execute(stmt)
        await self.session.commit()
