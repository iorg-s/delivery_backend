# app/main.py
from fastapi import FastAPI
from .db import engine, Base
from app.api.v1.endpoints import router as api_router
from app.api.v1.driver_routes import router as driver_routes_router
from app.auth import auth_router

app = FastAPI(title="Delivery App")
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(driver_routes_router)

@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)

@app.get("/health")
async def health():
    return {"status": "ok"}

app.include_router(api_router)
