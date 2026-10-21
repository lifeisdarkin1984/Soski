from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class CollectorSetting(Base):
    __tablename__ = "collector_settings"
    setting_key: Mapped[str] = mapped_column(String(50), primary_key=True)
    setting_value: Mapped[str] = mapped_column(Text, nullable=False)
