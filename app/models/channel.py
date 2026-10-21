from sqlalchemy import BigInteger, Integer, String, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from app.core.database import Base

class MonitoredChannel(Base):
    __tablename__ = "monitored_channels"
    __table_args__ = (
        UniqueConstraint("channel_id", "account_id", name="uq_channel_account"),
        Index("idx_channel_account", "account_id", "channel_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_username: Mapped[str] = mapped_column(String(100), nullable=True)
    channel_title: Mapped[str] = mapped_column(String(255), nullable=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    last_processed_msg_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())

    account: Mapped["Account"] = relationship(back_populates="channels")