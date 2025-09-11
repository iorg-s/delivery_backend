 # app/main.py
from fastapi import FastAPI
from .db import engine, Base
from app.api.v1.endpoints import router as api_router
from app.api.v1.driver_routes import router as driver_routes_router
from app.auth import auth_router

# --- Notifications ---
from app.notifications import router as notifications_router, start_watcher, stop_watcher

app = FastAPI(title="Delivery App")

# Auth
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Driver routes
app.include_router(driver_routes_router)

# API endpoints
app.include_router(api_router)

# Notifications WS
app.include_router(notifications_router)

@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    # start background watcher
    await start_watcher()

@app.on_event("shutdown")
async def shutdown_event():
    await stop_watcher()

@app.get("/health")
async def health():
    return {"status": "ok"}
