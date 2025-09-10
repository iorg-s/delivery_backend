from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, Field
from sqlalchemy import func

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

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

# MOYSKLAD

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")

def notify_moysklad(delivery_id: str, status: str):
    """Send status update to MoySklad"""
    if not MOYSKLAD_TOKEN:
        print("[MoySklad] No token configured, skipping update")
        return

    url = "https://api.moysklad.ru/api/remap/1.2/entity/customerorder"
    headers = {
        "Authorization": MOYSKLAD_TOKEN,
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }

    payload = {
        "delivery_id": delivery_id,
        "status": status,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[MoySklad] Updated delivery {delivery_id} -> {status}")
    except Exception as e:
        print(f"[MoySklad] Failed to update {delivery_id}: {e}")

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

    # Remove existing selections for today
    db.query(DriverRoute).filter(
        DriverRoute.driver_id == current_user.id,
        DriverRoute.route_date == today
    ).delete()

    # Always add the MAIN warehouse automatically
    main = db.query(Warehouse).filter(Warehouse.is_main == True).first()
    if main:
        route = DriverRoute(driver_id=current_user.id, warehouse_id=main.id, route_date=today)
        db.add(route)

    # Add driver-selected warehouses
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

    # Only return non-main warehouses (shops)
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
    # Parse selected shop IDs from query
    ids = [s for s in (shop_ids or "").split(",") if s]

    # Main warehouse id
    main_id = db.query(Warehouse.id).filter(Warehouse.is_main == True).scalar()

    q = (
        db.query(Delivery)
        .options(
            joinedload(Delivery.source),
            joinedload(Delivery.destination),
        )
    )

    # Build a destination-only filter:
    dest_filter = None
    if ids:
        dest_filter = Delivery.destination_id.in_(ids)
    if main_id:
        main_dest = (Delivery.destination_id == main_id)
        dest_filter = main_dest if dest_filter is None else (dest_filter | main_dest)

    if dest_filter is not None:
        q = q.filter(dest_filter)

    # 🚨 Drivers don’t see already received deliveries
    if current_user.role == "driver":
        q = q.filter(Delivery.status != DeliveryStatus.received)

    deliveries = q.all()

    # Build response with names + counters
    result = []
    for d in deliveries:
        scanned = sum(c.total for c in d.scan_counters) if d.scan_counters else 0

        # Safe source/destination name handling
        source_name = None
        if d.source:
            source_name = "Petricani" if d.source.is_main else d.source.name

        dest_name = None
        if d.destination:
            dest_name = "Petricani" if d.destination.is_main else d.destination.name

        result.append({
            "id": d.id,
            "delivery_number": d.delivery_number,
            "status": d.status.value,
            "expected_packages": d.expected_packages,
            "source_id": d.source_id,
            "destination_id": d.destination_id,
            "source_name": source_name or "Unknown",
            "destination_name": dest_name or "Unknown",
            "counters": {
                c.stage.value: c.total for c in d.scan_counters
            }
        })

    return result

# --------------------------
# Delivery creation (accept delivery_number from app)
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
    # Only managers can create deliveries
    if current_user.role not in (UserRole.manager, UserRole.supervisor):
        raise HTTPException(status_code=403, detail="Only managers can create deliveries")

    # Manager must have a warehouse assigned
    if not current_user.warehouse_id:
        raise HTTPException(status_code=400, detail="Manager has no warehouse assigned")

    # Check if delivery with this number already exists
    existing = db.query(Delivery).filter(Delivery.delivery_number == payload.delivery_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="Delivery number already exists")

    # Create delivery using scanned number
    delivery = Delivery(
        id=str(uuid.uuid4()),  # <- Python UUID
        delivery_number=payload.delivery_number,
        status=DeliveryStatus.created,
        expected_packages=payload.expected_packages,
        source_id=current_user.warehouse_id,
        destination_id=payload.destination_id,
    )
    db.add(delivery)
    db.flush()  # ensures delivery is inserted and ID exists in DB
    
    # Audit log
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
    # 1️⃣ Fetch delivery
    delivery = db.query(Delivery).filter(
        Delivery.delivery_number == scan.delivery_number
    ).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # 2️⃣ Role-based warehouse checks
    if current_user.role == "driver":
        today = datetime.utcnow().date()
        route_warehouses = db.query(DriverRoute.warehouse_id).filter(
            DriverRoute.driver_id == current_user.id,
            DriverRoute.route_date == today
        ).all()
        route_warehouses = [r[0] for r in route_warehouses]

        if delivery.source_id not in route_warehouses and delivery.destination_id not in route_warehouses:
            raise HTTPException(status_code=403, detail="Delivery not in your route today")

    # 3️⃣ Stage dependency validation
    stage = scan.stage
    if stage == ScanStage.dest_arrival:
        # Manager can only scan if driver fully picked
        if delivery.status != DeliveryStatus.picked:
            raise HTTPException(status_code=400, detail="Delivery must be fully picked before arrival scan")

    if stage == ScanStage.dest_receive:
        # Manager can scan if driver fully picked OR if it has already arrived
        if delivery.status not in [
            DeliveryStatus.picked,
            DeliveryStatus.arrived,
            DeliveryStatus.partial_receive
        ]:
            raise HTTPException(status_code=400, detail="Delivery must be picked/arrived first")

    # 4️⃣ Determine stage and get/create counter
    counter = db.query(ScanCounter).filter(
        ScanCounter.delivery_id == delivery.id,
        ScanCounter.stage == stage
    ).first()
    if not counter:
        counter = ScanCounter(delivery_id=delivery.id, stage=stage, total=0)
        db.add(counter)
        db.flush()

    # 🚫 Prevent overscan → capped
    new_total = min(counter.total + scan.count, delivery.expected_packages)

    # 5️⃣ Insert scan event (log only real increment)
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

    # 6️⃣ Update scan counter
    counter.total = new_total
    db.add(counter)

    # 7️⃣ Update delivery status with partials
    if increment > 0:
        if stage == ScanStage.source_pick:
            if new_total < delivery.expected_packages:
                delivery.status = DeliveryStatus.partial_pick
            else:
                delivery.status = DeliveryStatus.picked
                # 🔔 Notify MoySklad when fully picked
                # notify_moysklad(delivery.delivery_number, "picked")

        elif stage == ScanStage.dest_arrival:
            delivery.status = DeliveryStatus.arrived

        elif stage == ScanStage.dest_receive:
            # ✅ FIX: Only mark fully received when ALL packages are scanned,
            # otherwise keep it in partial_receive
            if new_total >= delivery.expected_packages:
                delivery.status = DeliveryStatus.received
                # 🔔 Notify MoySklad when fully received
                # notify_moysklad(delivery.delivery_number, "received")
            else:
                delivery.status = DeliveryStatus.partial_receive

        db.add(delivery)

    # 8️⃣ Audit log
    log = AuditLog(
        actor_id=current_user.id,
        event_type="scan",
        delivery_id=delivery.id,
        details={"stage": stage.value, "count": increment}
    )
    db.add(log)

    # 9️⃣ Commit
    db.commit()

    # 🔟 Collect counters per stage for response
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
