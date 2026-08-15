"""
Database models for the schedule service.
Uses SQLAlchemy with SQLite for simple deployment.
"""

from datetime import datetime
from typing import Optional, Any, Dict

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class Group(Base):
    """Model for storing group schedule data."""
    
    __tablename__ = "groups"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    title = Column(String(200), nullable=True)  # Human-readable title
    
    # Schedule data stored as JSON string
    schedule_json = Column(Text, nullable=True)
    
    # Generated ICS content (can be generated on-the-fly if not stored)
    ics_text = Column(Text, nullable=True)
    
    # Status tracking
    last_fetched_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    fetch_status = Column(String(50), default="pending")  # pending, fetching, ok, error
    
    # Version tracking for cache invalidation
    version = Column(Integer, default=0)
    
    # Rate limiting
    last_refresh_attempt = Column(DateTime, nullable=True)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
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


def create_database_engine(database_url: str = "sqlite+aiosqlite:///./schedule.db"):
    """Create database engine and tables."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    
    engine = create_async_engine(database_url, echo=False, future=True)
    
    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    return engine, async_session, init_db
