from sqlalchemy import String, Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from app.core.database import Base
import enum

class AccountStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    banned = "banned"

class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.active, server_default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["TelegramSession"] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")
    channels: Mapped[list["MonitoredChannel"]] = relationship(back_populates="account")

class TelegramSession(Base):
    __tablename__ = "telegram_sessions"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    session_string: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    account: Mapped["Account"] = relationship(back_populates="session")