from sqlalchemy import BigInteger, Integer, Enum, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from app.core.database import Base
import enum

class ProcessStatus(enum.Enum):
    pending = "pending"
    processing = "processing"
    scanned = "scanned"
    completed = "completed"
    failed = "failed"

class FlowStatus(enum.Enum):
    initiated = "initiated"
    interacting = "interacting"
    joined_forced = "joined_forced"
    completed = "completed"
    failed = "failed"

class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", "account_id", name="uq_msg"),
        Index("idx_msg_status_flow", "status", "flow_status"),
        Index("idx_msg_account_channel", "account_id", "channel_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bot_username: Mapped[str] = mapped_column(String(100), nullable=True)
    start_param: Mapped[str] = mapped_column(String(255), nullable=True)
    flow_status: Mapped[FlowStatus] = mapped_column(Enum(FlowStatus), default=FlowStatus.initiated, server_default="initiated")
    status: Mapped[ProcessStatus] = mapped_column(Enum(ProcessStatus), default=ProcessStatus.pending, server_default="pending")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())