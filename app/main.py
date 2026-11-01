 # app/main.py
import traceback
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import inspect, text
from .db import engine, Base
from app.api.v1.endpoints import router as api_router
from app.api.v1.driver_routes import router as driver_routes_router
from app.auth import auth_router
from app.api.v2.deliveries import router as deliveries_v2_router
from app.api.bridge import router as bridge_router

# --- Notifications ---
from app.notifications import router as notifications_router, start_watcher, stop_watcher

app = FastAPI(title="Delivery App")


def release_stale_delivery_locks() -> int:
    """End connections stuck idle inside a transaction that locks deliveries.

    Older instances' notification watcher kept one read transaction open for
    its whole lifetime. During a deploy that old instance is still running,
    and its lock blocks the migration's ALTER TABLE indefinitely. Only
    connections idle in a transaction for over a minute and holding a lock on
    deliveries are ended; normal requests never stay in that state. The
    watcher queries every few seconds inside that transaction, so its
    state_change keeps resetting: the transaction start is what is old.
    """
    if engine.dialect.name != "postgresql":
        return 0
    with engine.connect() as connection:
        ended = connection.execute(text("""
            SELECT pg_terminate_backend(a.pid)
            FROM pg_stat_activity a
            WHERE a.datname = current_database()
              AND a.pid <> pg_backend_pid()
              AND a.state = 'idle in transaction'
              AND a.xact_start < now() - interval '60 seconds'
              AND EXISTS (
                  SELECT 1 FROM pg_locks l
                  JOIN pg_class c ON c.oid = l.relation
                  WHERE l.pid = a.pid AND c.relname = 'deliveries'
              )
        """)).fetchall()
        connection.commit()
    print(f"[startup] ended {len(ended)} stale idle-in-transaction connection(s)")
    return len(ended)


def run_database_migrations() -> None:
    """Bring PostgreSQL to the code revision before accepting traffic."""
    release_stale_delivery_locks()
    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    inspector = inspect(engine)
    # Production predates Alembic and was originally initialized with
    # Base.metadata.create_all(). Stamp that known baseline only when its core
    # table exists and the Alembic ledger is absent or empty. Never overwrite a
    # real revision value.
    if inspector.has_table("deliveries"):
        has_version_table = inspector.has_table("alembic_version")
        current_revision = None
        if has_version_table:
            with engine.connect() as connection:
                current_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                ).scalar_one_or_none()
        if not current_revision:
            command.stamp(config, "17301793ba4f")
    command.upgrade(config, "head")

# Auth
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Driver routes
app.include_router(driver_routes_router)

# API endpoints
app.include_router(api_router)
app.include_router(deliveries_v2_router)
# Render <-> Firebase delivery bridge (header secret DELIVERY_BRIDGE_SECRET)
app.include_router(bridge_router)

# Notifications WS
app.include_router(notifications_router)

@app.on_event("startup")
async def startup_event():
    try:
        run_database_migrations()
    except Exception:
        # Alembic's logging config disables uvicorn's error logger, so print
        # the cause before the process exits.
        traceback.print_exc()
        raise
    Base.metadata.create_all(bind=engine)
    # start background watcher
    await start_watcher()

@app.on_event("shutdown")
async def shutdown_event():
    await stop_watcher()

@app.get("/health")
async def health():
    return {"status": "ok"}
