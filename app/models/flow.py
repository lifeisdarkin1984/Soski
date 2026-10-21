from sqlalchemy import BigInteger, Integer, String, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from app.core.database import Base

class FlowJoinChannel(Base):
    __tablename__ = "flow_join_channels"
    __table_args__ = (Index("idx_flow_join_msg", "processed_message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    processed_message_id: Mapped[int] = mapped_column(Integer, ForeignKey("processed_messages.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_username: Mapped[str] = mapped_column(String(100), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())