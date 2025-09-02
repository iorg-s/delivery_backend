# app/api/v1/driver_routes.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import DriverRoute, User
from app.auth import get_current_user

router = APIRouter()

@router.get("/driver_routes")
def get_driver_routes(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    routes = db.query(DriverRoute).filter(DriverRoute.driver_id == current_user.id).all()
    return [{"warehouse_id": r.warehouse_id, "route_date": r.route_date} for r in routes]
