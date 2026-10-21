from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from app.models.flow import FlowJoinChannel
from app.models.delivery import Delivery
from app.models.message import ProcessedMessage, ProcessStatus, FlowStatus
from app.core.logger import logger


class FlowRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_joined_channel(self, processed_msg_id: int, channel_id: int, channel_username: str):
        self.session.add(FlowJoinChannel(
            processed_message_id=processed_msg_id, channel_id=channel_id, channel_username=channel_username
        ))
        await self.session.commit()

    async def get_force_joined_channel_ids(self) -> set[int]:
        result = await self.session.execute(select(FlowJoinChannel.channel_id).where(FlowJoinChannel.channel_id != 0))
        return set(result.scalars().all())

    # ---------- Delivery: اول «رزرو»، بعد ارسال، بعد «ثبت نهایی» ----------
    async def claim_delivery(self, data: dict):
        """
        ('claimed', id)        رزرو شد؛ ارسال مجاز است
        ('already_sent', None) قبلاً ارسال شده
        ('busy', None)         درحال ارسال توسط کارگر دیگر یا وضعیت نامشخص؛ دوباره نفرست
        """
        self.session.add(Delivery(status="sending", **data))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            row = (await self.session.execute(
                select(Delivery).where(Delivery.file_unique_id == data["file_unique_id"])
            )).scalars().first()
            if row is not None and row.status == "sent":
                return "already_sent", None
            return "busy", None
        row = (await self.session.execute(
            select(Delivery.id).where(Delivery.file_unique_id == data["file_unique_id"])
        )).scalar_one()
        return "claimed", row

    async def mark_delivery_sent(self, delivery_id: int, destination_msg_id: int | None, report_text: str):
        await self.session.execute(update(Delivery).where(Delivery.id == delivery_id).values(
            status="sent", destination_msg_id=destination_msg_id, report_text=report_text))
        await self.session.commit()

    async def release_delivery(self, delivery_id: int):
        """ارسال قطعاً شکست خورده؛ رزرو آزاد می‌شود تا دفعه‌ی بعد بشود دوباره تلاش کرد."""
        await self.session.execute(delete(Delivery).where(Delivery.id == delivery_id, Delivery.status == "sending"))
        await self.session.commit()

    async def mark_delivery_unknown(self, delivery_id: int):
        await self.session.execute(update(Delivery).where(Delivery.id == delivery_id).values(status="unknown"))
        await self.session.commit()

    async def reconcile_sending_deliveries(self) -> int:
        """بعد از ری‌استارت: ارسال‌های نیمه‌تمام نتیجه‌شان معلوم نیست؛ دوباره ارسال نمی‌شوند."""
        res = await self.session.execute(
            update(Delivery).where(Delivery.status == "sending").values(status="unknown"))
        await self.session.commit()
        return res.rowcount or 0

    # ---------- وضعیت پیام ----------
    async def mark_message_processing(self, msg_id: int):
        await self.session.execute(update(ProcessedMessage).where(ProcessedMessage.id == msg_id).values(
            status=ProcessStatus.processing))
        await self.session.commit()

    async def mark_message_completed(self, msg_id: int):
        await self.session.execute(update(ProcessedMessage).where(ProcessedMessage.id == msg_id).values(
            status=ProcessStatus.completed, flow_status=FlowStatus.completed))
        await self.session.commit()

    async def mark_message_failed(self, msg_id: int, error: str):
        await self.session.execute(update(ProcessedMessage).where(ProcessedMessage.id == msg_id).values(
            status=ProcessStatus.failed, flow_status=FlowStatus.failed, error_message=(error or "")[:2000]))
        await self.session.commit()

    async def update_flow_status(self, msg_id: int, flow_status: FlowStatus):
        await self.session.execute(update(ProcessedMessage).where(ProcessedMessage.id == msg_id).values(
            flow_status=flow_status))
        await self.session.commit()
