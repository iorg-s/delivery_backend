# app/api/v1/endpoints.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime
from app.db import get_db
from app.models import (
    Delivery, ScanEvent, ScanCounter, AuditLog, User,
    ScanStage, DeliveryStatus
)
from app.auth import get_current_user
from app.api.v1.schemas import ScanRequest, TransferCreate, ScanStage


router = APIRouter(prefix="/deliveries", tags=["deliveries"])

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
    # get today date
        today = datetime.utcnow().date()
    # fetch driver's route for today
    route_warehouses = db.query(DriverRoute.warehouse_id).filter(
        DriverRoute.driver_id == current_user.id,
        DriverRoute.route_date == today
    ).all()
    route_warehouses = [r[0] for r in route_warehouses]

    # allow scan only if delivery source or destination is in route
    if delivery.source_id not in route_warehouses and delivery.destination_id not in route_warehouses:
        raise HTTPException(status_code=403, detail="Delivery not in your route today")
    # supervisors can scan anything

    # 3️⃣ Determine stage and counter
    stage = scan.stage
    counter = db.query(ScanCounter).filter(
        ScanCounter.delivery_id == delivery.id,
        ScanCounter.stage == stage
    ).first()
    if not counter:
        counter = ScanCounter(delivery_id=delivery.id, stage=stage, total=0)
        db.add(counter)
        db.flush()

    if counter.total + scan.count > delivery.expected_packages:
        raise HTTPException(status_code=400, detail="Cannot scan more packages than expected")

    # 4️⃣ Insert scan event
    scan_event = ScanEvent(
        delivery_id=delivery.id,
        stage=stage,
        scanned_by=current_user.id,
        warehouse_id=current_user.warehouse_id,
        count=scan.count,
        client_device_id=client_device_id,
        client_ts=datetime.utcnow()
    )
    db.add(scan_event)

    # 5️⃣ Update scan counter
    counter.total += scan.count
    db.add(counter)

    # 6️⃣ Update delivery status if needed
    if counter.total == delivery.expected_packages:
        if stage == ScanStage.source_pick:
            delivery.status = DeliveryStatus.picked
        elif stage == ScanStage.dest_arrival:
            delivery.status = DeliveryStatus.arrived
        elif stage == ScanStage.dest_receive:
            delivery.status = DeliveryStatus.received
    db.add(delivery)

    # 7️⃣ Add audit log
    log = AuditLog(
        actor_id=current_user.id,
        event_type="scan",
        delivery_id=delivery.id,
        details={
            "stage": stage.value,
            "count": scan.count
        }
    )
    db.add(log)

    # 8️⃣ Commit all changes
    db.commit()

    return {
        "message": "Scan recorded",
        "delivery_status": delivery.status.value,
        "total_scanned": counter.total
    }

@router.post("/transfer")
def create_transfer(
    transfer: TransferCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can create transfers")

    # fetch delivery
    delivery = db.query(Delivery).filter(Delivery.delivery_number == transfer.delivery_number).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # manager can only transfer deliveries from their warehouse
    if delivery.source_id != current_user.warehouse_id:
        raise HTTPException(status_code=403, detail="Cannot transfer delivery from another warehouse")

    # create transfer order
    transfer_order = TransferOrder(
        delivery_id=delivery.id,
        from_id=current_user.warehouse_id,
        to_id=transfer.destination_id,
        expected_packages=transfer.package_count,
        created_by=current_user.id,
        status="open"
    )
    db.add(transfer_order)

    # update delivery status to 'redirected'
    delivery.status = DeliveryStatus.redirected
    db.add(delivery)

    # add audit log
    log = AuditLog(
        actor_id=current_user.id,
        event_type="transfer_created",
        delivery_id=delivery.id,
        details={"to": transfer.destination_id, "package_count": transfer.package_count}
    )
    db.add(log)
    db.commit()

    return {"message": "Transfer created", "transfer_id": transfer_order.id}