# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()  # optional if you have a local .env file

DATABASE_URL = os.getenv(
    "DATABASE_URL",  # <--- this is the correct key
    "postgresql://delivery_user:delivery_pass@localhost:5432/delivery_db"  # fallback for local dev
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
