# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from dotenv import load_dotenv
load_dotenv()  # reads .env in project root if present

DATABASE_URL = os.getenv("postgresql://iorg:wqJUeb66yZtMphzE30ixgUhDoFfFd2Jf@dpg-d2q4m375r7bs73aa0h00-a/soling_deliveries_xybg", "postgresql://delivery_user:delivery_pass@localhost:5432/delivery_db")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependency for FastAPI endpoints
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
