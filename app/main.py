 # app/main.py
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from .db import engine, Base
from app.api.v1.endpoints import router as api_router
from app.api.v1.driver_routes import router as driver_routes_router
from app.auth import auth_router
from app.api.v2.deliveries import router as deliveries_v2_router

# --- Notifications ---
from app.notifications import router as notifications_router, start_watcher, stop_watcher

app = FastAPI(title="Delivery App")


def run_database_migrations() -> None:
    """Bring PostgreSQL to the code revision before accepting traffic."""
    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(config, "head")

# Auth
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Driver routes
app.include_router(driver_routes_router)

# API endpoints
app.include_router(api_router)
app.include_router(deliveries_v2_router)

# Notifications WS
app.include_router(notifications_router)

@app.on_event("startup")
async def startup_event():
    run_database_migrations()
    Base.metadata.create_all(bind=engine)
    # start background watcher
    await start_watcher()

@app.on_event("shutdown")
async def shutdown_event():
    await stop_watcher()

@app.get("/health")
async def health():
    return {"status": "ok"}
