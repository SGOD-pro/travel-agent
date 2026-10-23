from sqlalchemy.orm import Mapped, mapped_column
from src.config.base_model import Base

class JobModel(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(default="PENDING")
    
    # These three are now optional/dynamic
    destination: Mapped[str] = mapped_column(default="UNKNOWN")
    days: Mapped[int] = mapped_column(default=0)
    budget: Mapped[float] = mapped_column(default=0.0)
    
    source_url: Mapped[str] = mapped_column(nullable=True) # New column for the link
    itinerary: Mapped[str] = mapped_column(nullable=True) 

class OutboxModel(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int]
    status: Mapped[str] = mapped_column(default="UNPUBLISHED")