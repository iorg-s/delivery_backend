from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
import uuid
import os
import requests  

from app.db import get_db
from app.models import (
    Delivery, ScanEvent, ScanCounter, AuditLog, User,
    ScanStage, DeliveryStatus, DriverRoute, Warehouse, TransferOrder, UserRole
)
from app.auth import get_current_user
from app.api.v1.schemas import ScanRequest, TransferCreate
from app.firebase import send_push  # ← added import for firebase
from app.notifications import register_fcm_token

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

# ==================================
# MOYSKLAD
# ==================================

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")

def notify_moysklad(delivery_number: str, status: str):
    """Find move by delivery_number (name) and update its state"""
    if not MOYSKLAD_TOKEN:
        print("[MoySklad] No token configured, skipping update")
        return

    headers = {
        "Authorization": f"{MOYSKLAD_TOKEN}",
        "Accept": "application/json;charset=utf-8",
        "Content-Type": "application/json;charset=utf-8",
    }

    try:
        # 1️⃣ Find move by name (delivery number)
        search_url = f"https://api.moysklad.ru/api/remap/1.2/entity/move?filter=name={delivery_number}"
        search_resp = requests.get(search_url, headers=headers, timeout=10)
        search_resp.raise_for_status()
        data = search_resp.json()

        if not data.get("rows"):
            print(f"[MoySklad] No document found for delivery_number={delivery_number}")
            return

        move_id = data["rows"][0]["id"]

        # 2️⃣ Choose state based on status
        if status == "picked":
            state_id = "b1c03517-cd4b-11ed-0a80-0cdc000b3192"
        elif status == "received":
            state_id = "5c6d6d27-87d9-11ee-0a80-11e70046d7f0"
        else:
            print(f"[MoySklad] Unknown status {status}, skipping")
            return

        # 3️⃣ Update state
        update_url = f"https://api.moysklad.ru/api/remap/1.2/entity/move/{move_id}"
        payload = {
            "state": {
                "meta": {
                    "href": f"https://api.moysklad.ru/api/remap/1.2/entity/move/metadata/states/{state_id}",
                    "type": "state",
                    "mediaType": "application/json"
                }
            }
        }

        update_resp = requests.put(update_url, headers=headers, json=payload, timeout=10)
        update_resp.raise_for_status()
        print(f"[MoySklad] Updated delivery {delivery_number} -> {status}")

    except Exception as e:
        print(f"[MoySklad] Failed to update {delivery_number}: {e}")


# --------------------------
# Multi-shop selection
# --------------------------

class DriverRouteSelect(BaseModel):
    warehouse_ids: List[str]

@router.get("/available_warehouses")
def get_warehouses(db: Session = Depends(get_db)):
    """Return all warehouses except the main one."""
    warehouses = db.query(Warehouse).filter(Warehouse.is_main == False).all()
    return [{"id": w.id, "name": w.name, "is_main": w.is_main} for w in warehouses]

@router.post("/select_route")
def select_route(
    selection: DriverRouteSelect,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    today = date.today()
    db.query(DriverRoute).filter(
        DriverRoute.driver_id == current_user.id,
        DriverRoute.route_date == today
    ).delete()

    main = db.query(Warehouse).filter(Warehouse.is_main == True).first()
    if main:
        route = DriverRoute(driver_id=current_user.id, warehouse_id=main.id, route_date=today)
        db.add(route)

    for wid in selection.warehouse_ids:
        route = DriverRoute(driver_id=current_user.id, warehouse_id=wid, route_date=today)
        db.add(route)

    db.commit()
    return {"message": "Route saved", "selected_warehouses": selection.warehouse_ids}

@router.get("/driver_routes")
def get_driver_route(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    today = date.today()
    main_id = db.query(Warehouse.id).filter(Warehouse.is_main == True).scalar()
    routes = db.query(DriverRoute).filter(
        DriverRoute.driver_id == current_user.id,
        DriverRoute.route_date == today
    ).all()

    return [
        {"warehouse_id": r.warehouse_id}
        for r in routes if str(r.warehouse_id) != str(main_id)
    ]


# --------------------------
# Deliveries (with shop_ids filter)
# --------------------------
@router.get("")
def get_deliveries(
    shop_ids: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ids = [s for s in (shop_ids or "").split(",") if s]

    q = db.query(Delivery).options(
        joinedload(Delivery.source),
        joinedload(Delivery.destination),
    )

    if current_user.role in ("manager", "supervisor"):
        filter_ids = [str(current_user.warehouse_id)]
    elif ids:
        filter_ids = ids
    else:
        filter_ids = []

    if filter_ids:
        q = q.filter(
            or_(
                Delivery.destination_id.in_(filter_ids),
                Delivery.source_id.in_(filter_ids)
            )
        )

    if current_user.role == "driver":
        q = q.filter(Delivery.status != DeliveryStatus.received)

    deliveries = q.all()
    result = []
    for d in deliveries:
        scanned = sum(c.total for c in d.scan_counters) if d.scan_counters else 0
        source_name = "Petricani" if d.source and d.source.is_main else (d.source.name if d.source else "Unknown")
        dest_name = "Petricani" if d.destination and d.destination.is_main else (d.destination.name if d.destination else "Unknown")

        result.append({
            "id": d.id,
            "delivery_number": d.delivery_number,
            "status": d.status.value,
            "expected_packages": d.expected_packages,
            "source_id": d.source_id,
            "destination_id": d.destination_id,
            "source_name": source_name,
            "destination_name": dest_name,
            "counters": {c.stage.value: c.total for c in d.scan_counters} if d.scan_counters else {},
        })

    return result


# --------------------------
# Delivery creation
# --------------------------
class DeliveryCreate(BaseModel):
    destination_id: str
    expected_packages: int
    delivery_number: str = Field(..., description="Scanned delivery number from the app")

@router.post("")
def create_delivery(
    payload: DeliveryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.manager, UserRole.supervisor):
        raise HTTPException(status_code=403, detail="Only managers can create deliveries")

    if not current_user.warehouse_id:
        raise HTTPException(status_code=400, detail="Manager has no warehouse assigned")

    existing = db.query(Delivery).filter(Delivery.delivery_number == payload.delivery_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="Delivery number already exists")

    delivery = Delivery(
        id=str(uuid.uuid4()),
        delivery_number=payload.delivery_number,
        status=DeliveryStatus.created,
        expected_packages=payload.expected_packages,
        source_id=current_user.warehouse_id,
        destination_id=payload.destination_id,
    )
    db.add(delivery)
    db.flush()

    log = AuditLog(
        actor_id=current_user.id,
        event_type="delivery_created",
        delivery_id=delivery.id,
        details={
            "source": current_user.warehouse_id,
            "destination": payload.destination_id,
            "expected_packages": payload.expected_packages,
        },
    )
    db.add(log)
    db.commit()
    db.refresh(delivery)

    # 🔔 Send push notifications
    # Drivers: all deliveries
    # driver_tokens = [u.fcm_token for u in db.query(User).filter(User.role == "driver").all() if u.fcm_token]
    # for token in driver_tokens:
    #     send_push(token, "New delivery", f"Delivery {delivery.delivery_number} has been created")

    # # Managers: only deliveries to their warehouse
    # manager_tokens = [u.fcm_token for u in db.query(User).filter(User.role == "manager", User.warehouse_id == delivery.destination_id).all() if u.fcm_token]
    # for token in manager_tokens:
    #     send_push(token, "New delivery", f"Delivery {delivery.delivery_number} assigned to your warehouse")

    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "status": delivery.status.value,
        "expected_packages": delivery.expected_packages,
        "source_id": delivery.source_id,
        "destination_id": delivery.destination_id,
        "created": delivery.created_at.isoformat(),
    }

# --------------------------
# Delivery scanning
# --------------------------
@router.post("/scan")
def scan_delivery(
    scan: ScanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    client_device_id: str | None = None
):
    delivery = db.query(Delivery).filter(
        Delivery.delivery_number == scan.delivery_number
    ).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    if current_user.role == "driver":
        today = datetime.utcnow().date()
        route_warehouses = db.query(DriverRoute.warehouse_id).filter(
            DriverRoute.driver_id == current_user.id,
            DriverRoute.route_date == today
        ).all()
        route_warehouses = [r[0] for r in route_warehouses]

        if delivery.source_id not in route_warehouses and delivery.destination_id not in route_warehouses:
            raise HTTPException(status_code=403, detail="Delivery not in your route today")

    stage = scan.stage
    if stage == ScanStage.dest_arrival:
        if delivery.status != DeliveryStatus.picked:
            raise HTTPException(status_code=400, detail="Delivery must be fully picked before arrival scan")

    if stage == ScanStage.dest_receive:
        if delivery.status not in [
            DeliveryStatus.picked,
            DeliveryStatus.arrived,
            DeliveryStatus.partial_receive
        ]:
            raise HTTPException(status_code=400, detail="Delivery must be picked/arrived first")

    counter = db.query(ScanCounter).filter(
        ScanCounter.delivery_id == delivery.id,
        ScanCounter.stage == stage
    ).first()
    if not counter:
        counter = ScanCounter(delivery_id=delivery.id, stage=stage, total=0)
        db.add(counter)
        db.flush()

    new_total = min(counter.total + scan.count, delivery.expected_packages)
    increment = max(0, new_total - counter.total)

    if increment > 0:
        if current_user.role == "manager":
            warehouse_id = current_user.warehouse_id
        elif stage == ScanStage.source_pick:
            warehouse_id = delivery.source_id
        else:
            warehouse_id = delivery.destination_id

        scan_event = ScanEvent(
            delivery_id=delivery.id,
            stage=stage,
            scanned_by=current_user.id,
            warehouse_id=warehouse_id,
            count=increment,
            client_device_id=client_device_id,
            client_ts=datetime.utcnow()
        )
        db.add(scan_event)

    counter.total = new_total
    db.add(counter)

    if increment > 0:
        if stage == ScanStage.source_pick:
            if new_total < delivery.expected_packages:
                delivery.status = DeliveryStatus.partial_pick
            else:
                delivery.status = DeliveryStatus.picked
                notify_moysklad(delivery.delivery_number, "picked")

        elif stage == ScanStage.dest_arrival:
            delivery.status = DeliveryStatus.arrived

        elif stage == ScanStage.dest_receive:
            if new_total >= delivery.expected_packages:
                delivery.status = DeliveryStatus.received
                notify_moysklad(delivery.delivery_number, "received")
            else:
                delivery.status = DeliveryStatus.partial_receive

        db.add(delivery)

    log = AuditLog(
        actor_id=current_user.id,
        event_type="scan",
        delivery_id=delivery.id,
        details={"stage": stage.value, "count": increment}
    )
    db.add(log)

    db.commit()

    counters = {
        c.stage.value: c.total
        for c in db.query(ScanCounter).filter(ScanCounter.delivery_id == delivery.id).all()
    }

    return {
        "message": "Scan recorded",
        "delivery_status": delivery.status.value,
        "counters": counters,
        "expected_packages": delivery.expected_packages,
    }

# --------------------------
# Transfer
# --------------------------
@router.post("/transfer")
def create_transfer(
    transfer: TransferCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can create transfers")

    delivery = db.query(Delivery).filter(Delivery.delivery_number == transfer.delivery_number).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    if delivery.source_id != current_user.warehouse_id:
        raise HTTPException(status_code=403, detail="Cannot transfer delivery from another warehouse")

    transfer_order = TransferOrder(
        delivery_id=delivery.id,
        from_id=current_user.warehouse_id,
        to_id=transfer.destination_id,
        expected_packages=transfer.package_count,
        created_by=current_user.id,
        status="open"
    )
    db.add(transfer_order)
    delivery.status = DeliveryStatus.redirected
    db.add(delivery)

    log = AuditLog(
        actor_id=current_user.id,
        event_type="transfer_created",
        delivery_id=delivery.id,
        details={"to": transfer.destination_id, "package_count": transfer.package_count}
    )
    db.add(log)
    db.commit()

    return {"message": "Transfer created", "transfer_id": transfer_order.id}


@router.get("/all_warehouses")
def get_all_warehouses(db: Session = Depends(get_db)):
    """Return all warehouses, including main."""
    warehouses = db.query(Warehouse).all()
    return [{"id": w.id, "name": w.name, "is_main": w.is_main} for w in warehouses]

@router.post("/register_fcm")
def register_fcm(token: str, current_user: User = Depends(get_current_user)):
    register_fcm_token(current_user.id, token)
    return {"status": "ok"}
