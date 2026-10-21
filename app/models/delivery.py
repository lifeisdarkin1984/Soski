from sqlalchemy import BigInteger, Integer, String, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from app.core.database import Base

class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("file_unique_id", name="uq_delivery_file_unique"),
        Index("idx_delivery_account", "account_id"),
        Index("idx_delivery_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    source_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=True)
    target_bot_username: Mapped[str] = mapped_column(String(100), nullable=True)
    joined_channels_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    report_text: Mapped[str] = mapped_column(Text, nullable=True)
    file_id: Mapped[str] = mapped_column(String(500), nullable=True)
    file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # sending = در حال ارسال | sent = ارسال شد | unknown = نتیجه‌ی ارسال نامشخص (بعد از ری‌استارت)
    status: Mapped[str] = mapped_column(String(20), default="sent", server_default="sent", nullable=False)
    destination_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())