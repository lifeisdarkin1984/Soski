"""جدول‌های توزیع: ربات‌های دریافت، پست‌های کانال نهایی، عضویت اجباری و ویدیوهای تحویل‌شده به کاربران.
همه‌ی زمان‌ها به وقت UTC (بدون timezone) ذخیره می‌شوند."""
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Integer, String, Text, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReceiverBot(Base):
    __tablename__ = "receiver_bots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)       # شناسه‌ی عددی تلگرام
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)                         # توکن رمزنگاری‌شده
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_assigned_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # شمارنده‌ی چرخشی
    assigned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_health_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_health_ok: Mapped[bool] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class MediaPost(Base):
    __tablename__ = "media_posts"
    __table_args__ = (
        Index("idx_media_posts_status", "status", "next_attempt_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(Integer, ForeignKey("deliveries.id", ondelete="RESTRICT"),
                                             unique=True, nullable=False)
    token: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)         # بخش یکتای لینک اختصاصی
    storage_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    caption: Mapped[str] = mapped_column(Text, nullable=True)                           # کپشن تمیزشده‌ی پست اصلی
    final_caption: Mapped[str] = mapped_column(Text, nullable=True)
    caption_trimmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # pending | publishing | published | failed | unknown
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    assigned_bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("receiver_bots.id", ondelete="RESTRICT"),
                                                 nullable=True)
    final_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    final_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class ForcedChannel(Base):
    __tablename__ = "forced_channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    link: Mapped[str] = mapped_column(String(255), nullable=True)                        # لینک دکمه‌ی عضویت
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class VideoDelivery(Base):
    __tablename__ = "video_deliveries"
    __table_args__ = (
        Index("idx_vd_due", "status", "delete_at"),
        Index("idx_vd_user_media", "user_id", "media_post_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_post_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_posts.id", ondelete="RESTRICT"),
                                               nullable=False)
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("receiver_bots.id", ondelete="RESTRICT"),
                                        nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    video_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    guide_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    # sending | sent | unknown | send_failed | deleted | delete_failed
    status: Mapped[str] = mapped_column(String(20), default="sending", nullable=False)
    delete_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    delete_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
