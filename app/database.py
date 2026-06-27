import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Database URL configuration (falls keine DATABASE_URL gesetzt ist, nutzen wir eine lokale SQLite DB)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app/data/rddtscpr.db")

# Für SQLite müssen wir connect_args={"check_same_thread": False} hinzufügen
if DATABASE_URL.startswith("sqlite"):
    # Sicherstellen, dass das Verzeichnis existiert
    os.makedirs("./app/data", exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class APIRequestLog(Base):
    __tablename__ = "api_request_logs"

    id = Column(Integer, primary_key=True, index=True)
    endpoint = Column(String(50), nullable=False)  # e.g., "/v1/subreddit-posts"
    target = Column(String(255), nullable=False)   # e.g., "beziehungen" or Post-URL
    status_code = Column(Integer, nullable=False)
    response_time_ms = Column(Integer, nullable=False)
    method_used = Column(String(20), nullable=False)  # "json" or "playwright"
    proxy_used = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
