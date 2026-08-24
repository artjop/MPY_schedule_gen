"""
Модель БД сервиса расписания.

Одна строка = одна группа (общая сущность). Для группы хранится:
  - schedule_json — актуальный JSON расписания (один на группу);
  - ics_text      — общий .ics-календарь (один на группу).
Пользователи не создают отдельных копий — все подписываются на одну ссылку.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    title = Column(String(200), nullable=True)  # Человекочитаемое название группы

    # Актуальный JSON с расписанием группы (один на группу)
    schedule_json = Column(Text, nullable=True)

    # Общий .ics-календарь группы (один на группу)
    ics_text = Column(Text, nullable=True)

    last_fetched_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    fetch_status = Column(String(50), default="pending")  # pending, fetching, ok, error

    # Версия для инвалидации кэша / отслеживания обновлений
    version = Column(Integer, default=0)

    # Для rate limiting (кулдаун ручных обновлений)
    last_refresh_attempt = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для API-ответов."""
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title or self.slug,
            "last_fetched_at": self.last_fetched_at.isoformat() if self.last_fetched_at else None,
            "last_error": self.last_error,
            "fetch_status": self.fetch_status,
            "version": self.version,
            "has_schedule": self.schedule_json is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
