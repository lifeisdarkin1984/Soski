from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models.message import ProcessedMessage, ProcessStatus, FlowStatus
from app.core.logger import logger


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def is_message_processed(self, channel_id: int, message_id: int, account_id: int) -> bool:
        stmt = select(ProcessedMessage.id).where(
            ProcessedMessage.channel_id == channel_id,
            ProcessedMessage.message_id == message_id,
            ProcessedMessage.account_id == account_id
        )
        result = await self.session.execute(stmt)
        return result.first() is not None

    async def mark_as_pending_request(self, channel_id: int, message_id: int, account_id: int,
                                      bot_username: str, start_param: str | None = None):
        """برمی‌گرداند رکورد جدید؛ اگر قبلاً ثبت شده باشد None (یعنی کسی دیگر مسئول آن است)."""
        new_msg = ProcessedMessage(
            channel_id=channel_id, message_id=message_id, account_id=account_id,
            bot_username=bot_username, start_param=start_param,
            status=ProcessStatus.pending, flow_status=FlowStatus.initiated
        )
        self.session.add(new_msg)
        try:
            await self.session.commit()
            await self.session.refresh(new_msg)
            return new_msg
        except IntegrityError:
            await self.session.rollback()
            logger.info(f"Message {channel_id}/{message_id} already registered; skipping.")
            return None

    async def get_pending_or_stuck_messages(self, account_id: int, limit: int = 50):
        stmt = select(ProcessedMessage).where(
            ProcessedMessage.account_id == account_id,
            ProcessedMessage.status.in_([ProcessStatus.pending, ProcessStatus.processing]),
        ).order_by(ProcessedMessage.created_at.asc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def mark_as_scanned(self, channel_id: int, message_id: int, account_id: int):
        new_msg = ProcessedMessage(
            channel_id=channel_id, message_id=message_id, account_id=account_id,
            status=ProcessStatus.scanned, flow_status=FlowStatus.completed
        )
        self.session.add(new_msg)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()

    async def update_flow_status(self, msg_id: int, flow_status: FlowStatus):
        stmt = update(ProcessedMessage).where(ProcessedMessage.id == msg_id).values(flow_status=flow_status)
        await self.session.execute(stmt)
        await self.session.commit()

    async def requeue_failed(self, limit: int = 200) -> int:
        """پیام‌های ناموفق را دوباره به صف برمی‌گرداند (تلاش دستی مدیر)."""
        ids = (await self.session.execute(
            select(ProcessedMessage.id).where(ProcessedMessage.status == ProcessStatus.failed).limit(limit)
        )).scalars().all()
        if not ids:
            return 0
        await self.session.execute(
            update(ProcessedMessage).where(ProcessedMessage.id.in_(ids)).values(
                status=ProcessStatus.pending, flow_status=FlowStatus.initiated, error_message=None)
        )
        await self.session.commit()
        return len(ids)
