from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ...auth_v2 import FirebasePrincipal, get_firebase_principal, require_roles
from ...db import SessionLocal, get_db
from ...models import (
    AuditLog,
    Delivery,
    DeliveryDeletionAudit,
    DeliveryEvent,
    DeliveryStatus,
    FirebaseDriverRoute,
    ScanCounter,
    ScanEvent,
    ScanStage,
    TransferOrder,
    Warehouse,
)
from ...services.moysklad_delivery import enrich_delivery, find_delivery_documents
from ..v1.endpoints import notify_moysklad


router = APIRouter(prefix="/api/v2/deliveries", tags=["deliveries-v2"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def route_day_start() -> datetime:
    """Use the warehouse's local calendar day, not Render's UTC date."""
    local_day = datetime.now(ZoneInfo("Europe/Chisinau")).date()
    return datetime.combine(local_day, time.min)


def status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def counter_map(delivery: Delivery) -> Dict[str, int]:
    return {
        status_value(row.stage): int(row.total or 0)
        for row in (delivery.scan_counters or [])
    }


def warehouse_name(db: Session, warehouse_id: Optional[str]) -> str:
    if not warehouse_id:
        return ""
    row = db.query(Warehouse).filter(Warehouse.id == warehouse_id).one_or_none()
    return row.name if row else ""


def serialize_delivery(db: Session, delivery: Delivery) -> Dict[str, Any]:
    counters = counter_map(delivery)
    return {
        "id": delivery.id,
        "deliveryNumber": delivery.delivery_number,
        "type": delivery.delivery_type or "standard",
        "handoverStatus": delivery.handover_status,
        "status": status_value(delivery.status),
        "sourceId": delivery.source_id,
        "sourceName": delivery.source_name_snapshot or warehouse_name(db, delivery.source_id),
        "destinationId": delivery.destination_id,
        "destinationName": delivery.destination_name_snapshot or warehouse_name(db, delivery.destination_id),
        "expectedPackages": int(delivery.expected_packages or 0),
        "counters": counters,
        "currentLocationName": delivery.current_location_name,
        "placement": delivery.placement,
        "comment": delivery.comment,
        "assignedDriverUid": delivery.assigned_driver_uid,
        "assignedDriverName": delivery.assigned_driver_name,
        "arrivedAt": delivery.arrived_at.isoformat() if delivery.arrived_at else None,
        "arrivedByUid": delivery.arrived_by_uid,
        "arrivedByName": delivery.arrived_by_name,
        "completedAt": delivery.completed_at.isoformat() if delivery.completed_at else None,
        "completedByUid": delivery.completed_by_uid,
        "completedByName": delivery.completed_by_name,
        "archivedAt": delivery.archived_at.isoformat() if delivery.archived_at else None,
        "companyName": delivery.company_name,
        "itemSummary": delivery.item_summary,
        "moyskladDocumentType": delivery.moysklad_document_type,
        "moyskladDocumentId": delivery.moysklad_document_id,
        "moyskladDocumentName": delivery.moysklad_document_name,
        "enrichmentStatus": delivery.enrichment_status,
        "enrichmentError": delivery.enrichment_error,
        "enrichmentAttempts": int(delivery.enrichment_attempts or 0),
        "createdAt": delivery.created_at.isoformat() if delivery.created_at else None,
        "updatedAt": delivery.updated_at.isoformat() if delivery.updated_at else None,
        "version": int(delivery.version or 1),
    }


def load_delivery(db: Session, number_or_id: str, for_update: bool = False) -> Delivery:
    filters = [Delivery.delivery_number == number_or_id]
    try:
        UUID(str(number_or_id))
        filters.append(Delivery.id == number_or_id)
    except (TypeError, ValueError, AttributeError):
        pass
    query = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(or_(*filters))
    if for_update:
        query = query.with_for_update()
    delivery = query.one_or_none()
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


def ensure_client_event_is_new(
    db: Session,
    principal: FirebasePrincipal,
    client_event_id: Optional[str],
) -> Optional[DeliveryEvent]:
    if not client_event_id:
        return None
    return db.query(DeliveryEvent).filter(
        DeliveryEvent.actor_uid == principal.uid,
        DeliveryEvent.client_event_id == client_event_id,
    ).one_or_none()


def record_event(
    db: Session,
    delivery: Delivery,
    principal: FirebasePrincipal,
    event_type: str,
    count: int = 0,
    warehouse_id: Optional[str] = None,
    client_event_id: Optional[str] = None,
    client_device_id: Optional[str] = None,
    client_ts: Optional[datetime] = None,
    details: Optional[Dict[str, Any]] = None,
) -> DeliveryEvent:
    row = DeliveryEvent(
        delivery_id=delivery.id,
        event_type=event_type,
        actor_uid=principal.uid,
        actor_name=principal.name,
        actor_role=principal.role,
        warehouse_id=warehouse_id,
        count=count,
        client_event_id=client_event_id,
        client_device_id=client_device_id,
        client_ts=client_ts,
        details=details or {},
    )
    db.add(row)
    return row


def schedule_enrichment(delivery_id: str) -> None:
    db = SessionLocal()
    try:
        enrich_delivery(db, delivery_id)
    finally:
        db.close()


class RouteUpdate(BaseModel):
    warehouseIds: List[str] = Field(default_factory=list)


class DeliveryCreate(BaseModel):
    deliveryNumber: str
    expectedPackages: int = 1
    sourceId: Optional[str] = None
    sourceName: Optional[str] = None
    destinationId: str
    destinationName: Optional[str] = None
    type: str = "standard"
    placement: Optional[str] = None
    comment: Optional[str] = None
    moyskladMatch: Optional[Dict[str, Any]] = None


class ScanRequestV2(BaseModel):
    deliveryNumber: str
    action: Literal["pickup", "arrival_main", "broderie"]
    count: int = 1
    expectedPackages: Optional[int] = None
    clientEventId: Optional[str] = None
    clientDeviceId: Optional[str] = None
    clientTs: Optional[datetime] = None


class ReceiveRequest(BaseModel):
    deliveryNumber: str
    count: int = 1
    clientEventId: Optional[str] = None
    clientDeviceId: Optional[str] = None


class CompleteRequest(BaseModel):
    deliveryNumber: str
    force: bool = False
    reason: Optional[str] = None
    clientEventId: Optional[str] = None


class DeliveryPatch(BaseModel):
    expectedPackages: Optional[int] = None
    sourceId: Optional[str] = None
    sourceName: Optional[str] = None
    destinationId: Optional[str] = None
    destinationName: Optional[str] = None
    type: Optional[str] = None
    placement: Optional[str] = None
    comment: Optional[str] = None
    status: Optional[str] = None


class BulkUpdateRequest(BaseModel):
    ids: List[str]
    patch: DeliveryPatch


class DeleteRequest(BaseModel):
    ids: List[str]
    reason: Optional[str] = None


@router.get("/warehouses")
def list_warehouses(
    db: Session = Depends(get_db),
    _: FirebasePrincipal = Depends(get_firebase_principal),
):
    rows = db.query(Warehouse).order_by(Warehouse.is_main.desc(), Warehouse.name.asc()).all()
    return [{"id": row.id, "code": row.code, "name": row.name, "isMain": bool(row.is_main)} for row in rows]


@router.get("/route")
def get_route(
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["driver", "supervisor"])
    start = route_day_start()
    rows = db.query(FirebaseDriverRoute).filter(
        FirebaseDriverRoute.actor_uid == principal.uid,
        FirebaseDriverRoute.route_date == start,
    ).all()
    return {"warehouseIds": [row.warehouse_id for row in rows]}


@router.put("/route")
def set_route(
    payload: RouteUpdate,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["driver", "supervisor"])
    ids = list(dict.fromkeys(str(x).strip() for x in payload.warehouseIds if str(x).strip()))
    existing = db.query(Warehouse.id).filter(Warehouse.id.in_(ids)).all() if ids else []
    found = {row[0] for row in existing}
    missing = [value for value in ids if value not in found]
    if missing:
        raise HTTPException(status_code=422, detail={"unknownWarehouseIds": missing})
    start = route_day_start()
    db.query(FirebaseDriverRoute).filter(
        FirebaseDriverRoute.actor_uid == principal.uid,
        FirebaseDriverRoute.route_date == start,
    ).delete(synchronize_session=False)
    for warehouse_id in ids:
        db.add(FirebaseDriverRoute(
            actor_uid=principal.uid,
            actor_name=principal.name,
            route_date=start,
            warehouse_id=warehouse_id,
        ))
    db.commit()
    return {"ok": True, "warehouseIds": ids}


@router.get("/active")
def list_active(
    search: str = "",
    delivery_type: Optional[str] = Query(default=None, alias="type"),
    limit: int = Query(default=400, ge=1, le=1000),
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    query = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(Delivery.archived_at.is_(None))
    if principal.role == "manager" and principal.warehouse_id:
        query = query.filter(or_(
            Delivery.source_id == principal.warehouse_id,
            Delivery.destination_id == principal.warehouse_id,
        ))
    if principal.role == "driver":
        start = route_day_start()
        route_ids = {row[0] for row in db.query(FirebaseDriverRoute.warehouse_id).filter(
            FirebaseDriverRoute.actor_uid == principal.uid,
            FirebaseDriverRoute.route_date == start,
        ).all()}
        query = query.filter(or_(
            Delivery.delivery_type == "broderie",
            Delivery.source_id.in_(route_ids),
            Delivery.destination_id.in_(route_ids),
        ))
    if delivery_type:
        query = query.filter(Delivery.delivery_type == delivery_type)
    text_value = search.strip()
    if text_value:
        needle = f"%{text_value}%"
        query = query.filter(or_(
            Delivery.delivery_number.ilike(needle),
            Delivery.company_name.ilike(needle),
            Delivery.source_name_snapshot.ilike(needle),
            Delivery.destination_name_snapshot.ilike(needle),
            cast(Delivery.item_summary, String).ilike(needle),
        ))
    rows = query.order_by(Delivery.updated_at.desc()).limit(limit).all()
    return {"rows": [serialize_delivery(db, row) for row in rows]}


@router.get("/completed")
def list_completed(
    search: str = "",
    limit: int = Query(default=250, ge=1, le=1000),
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    query = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(Delivery.archived_at.is_not(None))
    if principal.role == "manager" and principal.warehouse_id:
        query = query.filter(or_(
            Delivery.source_id == principal.warehouse_id,
            Delivery.destination_id == principal.warehouse_id,
        ))
    if principal.role == "driver":
        query = query.filter(or_(
            Delivery.assigned_driver_uid == principal.uid,
            Delivery.arrived_by_uid == principal.uid,
        ))
    if search.strip():
        needle = f"%{search.strip()}%"
        query = query.filter(or_(
            Delivery.delivery_number.ilike(needle),
            Delivery.company_name.ilike(needle),
            cast(Delivery.item_summary, String).ilike(needle),
        ))
    rows = query.order_by(Delivery.archived_at.desc()).limit(limit).all()
    return {"rows": [serialize_delivery(db, row) for row in rows]}


@router.get("/{number_or_id}")
def delivery_detail(
    number_or_id: str,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    delivery = load_delivery(db, number_or_id)
    if (
        principal.role == "manager"
        and principal.warehouse_id
        and principal.warehouse_id not in {str(delivery.source_id), str(delivery.destination_id)}
    ):
        raise HTTPException(status_code=403, detail="Delivery is outside the manager's warehouse")
    if principal.role == "driver":
        route_ids = {row[0] for row in db.query(FirebaseDriverRoute.warehouse_id).filter(
            FirebaseDriverRoute.actor_uid == principal.uid,
            FirebaseDriverRoute.route_date == route_day_start(),
        ).all()}
        can_view = (
            delivery.delivery_type == "broderie"
            or delivery.assigned_driver_uid == principal.uid
            or delivery.arrived_by_uid == principal.uid
            or delivery.source_id in route_ids
            or delivery.destination_id in route_ids
        )
        if not can_view:
            raise HTTPException(status_code=403, detail="Delivery is outside today's route")
    events = db.query(DeliveryEvent).filter(DeliveryEvent.delivery_id == delivery.id).order_by(DeliveryEvent.created_at.asc()).all()
    data = serialize_delivery(db, delivery)
    data["events"] = [{
        "id": row.id,
        "type": row.event_type,
        "actorUid": row.actor_uid,
        "actorName": row.actor_name,
        "actorRole": row.actor_role,
        "count": row.count,
        "warehouseId": row.warehouse_id,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "details": row.details or {},
    } for row in events]
    return data


@router.get("/moysklad/preview/{number}")
def moysklad_preview(
    number: str,
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["manager", "supervisor"])
    matches = find_delivery_documents(number)
    return {"number": number, "matches": matches, "ambiguous": len(matches) > 1}


@router.post("")
def create_delivery(
    payload: DeliveryCreate,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["manager", "supervisor"])
    number = payload.deliveryNumber.strip()
    if not number:
        raise HTTPException(status_code=422, detail="Delivery number is required")
    if payload.expectedPackages <= 0 or payload.expectedPackages > 999:
        raise HTTPException(status_code=422, detail="Expected packages must be 1..999")
    if db.query(Delivery.id).filter(Delivery.delivery_number == number).first():
        raise HTTPException(status_code=409, detail="Delivery already exists")

    source_id = (payload.sourceId or principal.warehouse_id or "").strip()
    destination_id = payload.destinationId.strip()
    if not source_id or not destination_id:
        raise HTTPException(status_code=422, detail="Source and destination are required")
    warehouses = db.query(Warehouse).filter(Warehouse.id.in_([source_id, destination_id])).all()
    if {row.id for row in warehouses} != {source_id, destination_id}:
        raise HTTPException(status_code=422, detail="Unknown source or destination warehouse")
    # Role-only Firebase profiles intentionally have no legacy warehouse link.
    # In that case the manager chooses the source shop. If a supervisor later
    # assigns a PostgreSQL warehouse id to the Firebase profile, keep the
    # stronger one-shop restriction automatically.
    if (
        principal.role == "manager"
        and principal.warehouse_id
        and principal.warehouse_id != source_id
    ):
        raise HTTPException(status_code=403, detail="Manager may create only from assigned warehouse")

    match = payload.moyskladMatch or {}
    delivery = Delivery(
        delivery_number=number,
        source_id=source_id,
        destination_id=destination_id,
        expected_packages=payload.expectedPackages,
        status=DeliveryStatus.created,
        comment=payload.comment,
        delivery_type=payload.type.strip() or "standard",
        source_name_snapshot=payload.sourceName or warehouse_name(db, source_id),
        destination_name_snapshot=payload.destinationName or warehouse_name(db, destination_id),
        current_location_name=payload.sourceName or warehouse_name(db, source_id),
        placement=payload.placement,
        moysklad_document_type=match.get("documentType"),
        moysklad_document_id=match.get("documentId"),
        moysklad_document_name=match.get("documentNumber"),
        company_name=match.get("companyName"),
        item_summary={"items": match.get("items") or [], "remaining": match.get("remainingItems") or 0} if match else None,
        enrichment_status="completed" if match else "not_requested",
    )
    db.add(delivery)
    db.flush()
    record_event(db, delivery, principal, "created", details={"source": "manual_or_preview"})
    db.commit()
    db.refresh(delivery)
    return serialize_delivery(db, load_delivery(db, delivery.id))


@router.post("/scan")
def scan_delivery(
    payload: ScanRequestV2,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["driver"])
    number = payload.deliveryNumber.strip()
    if not number or payload.count <= 0:
        raise HTTPException(status_code=422, detail="Valid number and count are required")

    duplicate = ensure_client_event_is_new(db, principal, payload.clientEventId)
    if duplicate:
        delivery = load_delivery(db, duplicate.delivery_id)
        return {"ok": True, "duplicate": True, "delivery": serialize_delivery(db, delivery)}

    delivery = db.query(Delivery).filter(Delivery.delivery_number == number).with_for_update().one_or_none()

    if payload.action == "broderie" and delivery is None:
        places = int(payload.expectedPackages or 0)
        if places <= 0 or places > 999:
            raise HTTPException(status_code=422, detail="Places are required for a new embroidery delivery")
        main = db.query(Warehouse).filter(Warehouse.is_main.is_(True)).first()
        if main is None:
            raise HTTPException(status_code=409, detail="Main warehouse is not configured")
        delivery = Delivery(
            delivery_number=number,
            source_id=main.id,
            destination_id=main.id,
            expected_packages=places,
            status=DeliveryStatus.picked,
            delivery_type="broderie",
            handover_status="out",
            source_name_snapshot=main.name,
            destination_name_snapshot="Подрядчик по вышивке",
            current_location_name="У подрядчика по вышивке",
            assigned_driver_uid=principal.uid,
            assigned_driver_name=principal.name,
            enrichment_status="pending",
        )
        db.add(delivery)
        db.flush()
        record_event(
            db, delivery, principal, "broderie_out", count=places,
            warehouse_id=main.id, client_event_id=payload.clientEventId,
            client_device_id=payload.clientDeviceId, client_ts=payload.clientTs,
        )
        db.commit()
        background_tasks.add_task(schedule_enrichment, delivery.id)
        return {"ok": True, "message": "Передано на вышивку", "delivery": serialize_delivery(db, load_delivery(db, delivery.id))}

    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if delivery.archived_at is not None:
        raise HTTPException(status_code=409, detail="Delivery is already completed")

    if payload.action == "broderie":
        if delivery.delivery_type != "broderie":
            raise HTTPException(status_code=409, detail="Delivery is not embroidery")
        last = db.query(DeliveryEvent).filter(
            DeliveryEvent.delivery_id == delivery.id,
            DeliveryEvent.event_type.in_(["broderie_out", "broderie_back"]),
        ).order_by(DeliveryEvent.created_at.desc()).first()
        if delivery.handover_status == "out":
            if last and last.created_at and utcnow() - last.created_at < timedelta(minutes=30):
                left = timedelta(minutes=30) - (utcnow() - last.created_at)
                raise HTTPException(status_code=409, detail=f"Repeat scan blocked for {max(1, int(left.total_seconds() // 60) + 1)} minutes")
            delivery.handover_status = "back"
            delivery.status = DeliveryStatus.received
            delivery.current_location_name = warehouse_name(db, delivery.source_id)
            delivery.completed_at = utcnow()
            delivery.completed_by_uid = principal.uid
            delivery.completed_by_name = principal.name
            delivery.archived_at = utcnow()
            event_type = "broderie_back"
            message = "Вышивка вернулась"
        else:
            delivery.handover_status = "out"
            delivery.status = DeliveryStatus.picked
            delivery.current_location_name = "У подрядчика по вышивке"
            event_type = "broderie_out"
            message = "Передано на вышивку"
        delivery.assigned_driver_uid = principal.uid
        delivery.assigned_driver_name = principal.name
        delivery.version = int(delivery.version or 1) + 1
        record_event(db, delivery, principal, event_type, count=delivery.expected_packages, warehouse_id=delivery.source_id,
                     client_event_id=payload.clientEventId, client_device_id=payload.clientDeviceId, client_ts=payload.clientTs)
        db.commit()
        return {"ok": True, "message": message, "delivery": serialize_delivery(db, load_delivery(db, delivery.id))}

    start = route_day_start()
    route_ids = {row[0] for row in db.query(FirebaseDriverRoute.warehouse_id).filter(
        FirebaseDriverRoute.actor_uid == principal.uid,
        FirebaseDriverRoute.route_date == start,
    ).all()}
    if not route_ids or (delivery.source_id not in route_ids and delivery.destination_id not in route_ids):
        raise HTTPException(status_code=403, detail="Delivery is not in today's route")

    if payload.action == "pickup":
        if status_value(delivery.status) not in {"created", "ready_to_pick", "partial_pick"}:
            raise HTTPException(status_code=409, detail="Delivery cannot be picked up in its current status")
        counter = db.query(ScanCounter).filter(
            ScanCounter.delivery_id == delivery.id,
            ScanCounter.stage == ScanStage.source_pick,
        ).with_for_update().one_or_none()
        if counter is None:
            counter = ScanCounter(delivery_id=delivery.id, stage=ScanStage.source_pick, total=0)
            db.add(counter)
        after = int(counter.total or 0) + payload.count
        if after > delivery.expected_packages:
            raise HTTPException(status_code=409, detail="Cannot scan more than expected places")
        counter.total = after
        delivery.status = DeliveryStatus.picked if after == delivery.expected_packages else DeliveryStatus.partial_pick
        delivery.assigned_driver_uid = principal.uid
        delivery.assigned_driver_name = principal.name
        delivery.current_location_name = f"У водителя: {principal.name}" if after == delivery.expected_packages else delivery.source_name_snapshot
        event_type = "pickup"
        message = f"Загружено {after}/{delivery.expected_packages}"
        event_warehouse = delivery.source_id
        if after == delivery.expected_packages:
            # Preserve the legacy move-state integration while old and new
            # clients operate on the same rows.
            background_tasks.add_task(notify_moysklad, delivery.delivery_number, "picked")
    else:
        destination = db.query(Warehouse).filter(Warehouse.id == delivery.destination_id).one_or_none()
        picked = db.query(ScanCounter).filter(
            ScanCounter.delivery_id == delivery.id,
            ScanCounter.stage == ScanStage.source_pick,
        ).one_or_none()
        if not destination or not destination.is_main:
            raise HTTPException(status_code=409, detail="Arrival mode is only for main warehouse deliveries")
        if not picked or picked.total < delivery.expected_packages:
            raise HTTPException(status_code=409, detail="All places must be picked up first")
        if delivery.arrived_at:
            raise HTTPException(status_code=409, detail="Arrival was already recorded")
        delivery.status = DeliveryStatus.arrived
        delivery.arrived_at = utcnow()
        delivery.arrived_by_uid = principal.uid
        delivery.arrived_by_name = principal.name
        delivery.current_location_name = destination.name
        event_type = "arrival_main"
        message = f"Прибытие записано: {principal.name}"
        event_warehouse = delivery.destination_id

    delivery.version = int(delivery.version or 1) + 1
    record_event(db, delivery, principal, event_type, count=payload.count, warehouse_id=event_warehouse,
                 client_event_id=payload.clientEventId, client_device_id=payload.clientDeviceId, client_ts=payload.clientTs)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if payload.clientEventId:
            return {"ok": True, "duplicate": True, "delivery": serialize_delivery(db, load_delivery(db, number))}
        raise exc
    return {"ok": True, "message": message, "delivery": serialize_delivery(db, load_delivery(db, delivery.id))}


@router.post("/receive")
def receive_delivery(
    payload: ReceiveRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["manager", "supervisor"])
    duplicate = ensure_client_event_is_new(db, principal, payload.clientEventId)
    if duplicate:
        return {"ok": True, "duplicate": True, "delivery": serialize_delivery(db, load_delivery(db, duplicate.delivery_id))}
    delivery = load_delivery(db, payload.deliveryNumber, for_update=True)
    if delivery.archived_at:
        return {"ok": True, "alreadyCompleted": True, "delivery": serialize_delivery(db, delivery)}
    destination = db.query(Warehouse).filter(Warehouse.id == delivery.destination_id).one()
    if destination.is_main and principal.role != "supervisor":
        raise HTTPException(status_code=403, detail="Main warehouse deliveries require supervisor completion")
    if (
        principal.role == "manager"
        and principal.warehouse_id
        and principal.warehouse_id != str(delivery.destination_id)
    ):
        raise HTTPException(status_code=403, detail="Delivery is not for manager's warehouse")
    picked = counter_map(delivery).get(ScanStage.source_pick.value, 0)
    if picked < delivery.expected_packages:
        raise HTTPException(status_code=409, detail="Delivery is not fully picked up")
    counter = db.query(ScanCounter).filter(
        ScanCounter.delivery_id == delivery.id,
        ScanCounter.stage == ScanStage.dest_receive,
    ).with_for_update().one_or_none()
    if counter is None:
        counter = ScanCounter(delivery_id=delivery.id, stage=ScanStage.dest_receive, total=0)
        db.add(counter)
    after = int(counter.total or 0) + payload.count
    if after > delivery.expected_packages:
        raise HTTPException(status_code=409, detail="Cannot receive more than expected places")
    counter.total = after
    if after == delivery.expected_packages:
        delivery.status = DeliveryStatus.received
        delivery.completed_at = utcnow()
        delivery.completed_by_uid = principal.uid
        delivery.completed_by_name = principal.name
        delivery.archived_at = utcnow()
        background_tasks.add_task(notify_moysklad, delivery.delivery_number, "received")
    else:
        delivery.status = DeliveryStatus.partial_receive
    delivery.current_location_name = destination.name
    delivery.version = int(delivery.version or 1) + 1
    record_event(db, delivery, principal, "receive", count=payload.count, warehouse_id=delivery.destination_id,
                 client_event_id=payload.clientEventId, client_device_id=payload.clientDeviceId)
    db.commit()
    return {"ok": True, "message": f"Принято {after}/{delivery.expected_packages}", "delivery": serialize_delivery(db, load_delivery(db, delivery.id))}


@router.post("/complete")
def supervisor_complete(
    payload: CompleteRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["supervisor"])
    duplicate = ensure_client_event_is_new(db, principal, payload.clientEventId)
    if duplicate:
        return {"ok": True, "duplicate": True, "delivery": serialize_delivery(db, load_delivery(db, duplicate.delivery_id))}
    delivery = load_delivery(db, payload.deliveryNumber, for_update=True)
    if delivery.archived_at:
        return {"ok": True, "alreadyCompleted": True, "delivery": serialize_delivery(db, delivery)}
    is_ready = delivery.arrived_at is not None or delivery.delivery_type == "broderie"
    if not is_ready and not payload.force:
        raise HTTPException(status_code=409, detail="Arrival is required; use force with a reason to override")
    if payload.force and not (payload.reason or "").strip():
        raise HTTPException(status_code=422, detail="Reason is required for forced completion")
    delivery.status = DeliveryStatus.received
    delivery.completed_at = utcnow()
    delivery.completed_by_uid = principal.uid
    delivery.completed_by_name = principal.name
    delivery.archived_at = utcnow()
    background_tasks.add_task(notify_moysklad, delivery.delivery_number, "received")
    delivery.version = int(delivery.version or 1) + 1
    record_event(db, delivery, principal, "supervisor_complete", client_event_id=payload.clientEventId,
                 details={"forced": payload.force, "reason": payload.reason})
    db.commit()
    return {"ok": True, "delivery": serialize_delivery(db, load_delivery(db, delivery.id))}


def apply_patch(db: Session, delivery: Delivery, patch: DeliveryPatch) -> None:
    data = patch.model_dump(exclude_none=True)
    if "expectedPackages" in data:
        total = int(data["expectedPackages"])
        scanned_max = max(counter_map(delivery).values(), default=0)
        if total <= 0 or total > 999 or total < scanned_max:
            raise HTTPException(status_code=422, detail=f"Invalid places for {delivery.delivery_number}")
        delivery.expected_packages = total
    if "sourceId" in data:
        if not db.query(Warehouse.id).filter(Warehouse.id == data["sourceId"]).first():
            raise HTTPException(status_code=422, detail=f"Unknown source for {delivery.delivery_number}")
        delivery.source_id = data["sourceId"]
    if "destinationId" in data:
        if not db.query(Warehouse.id).filter(Warehouse.id == data["destinationId"]).first():
            raise HTTPException(status_code=422, detail=f"Unknown destination for {delivery.delivery_number}")
        delivery.destination_id = data["destinationId"]
    if "sourceName" in data:
        delivery.source_name_snapshot = data["sourceName"]
    if "destinationName" in data:
        delivery.destination_name_snapshot = data["destinationName"]
    if "type" in data:
        delivery.delivery_type = data["type"]
    if "placement" in data:
        delivery.placement = data["placement"]
    if "comment" in data:
        delivery.comment = data["comment"]
    if "status" in data:
        try:
            delivery.status = DeliveryStatus(data["status"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid status for {delivery.delivery_number}") from exc
    delivery.version = int(delivery.version or 1) + 1


@router.patch("/{number_or_id}")
def update_delivery(
    number_or_id: str,
    patch: DeliveryPatch,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["supervisor"])
    delivery = load_delivery(db, number_or_id, for_update=True)
    apply_patch(db, delivery, patch)
    record_event(db, delivery, principal, "supervisor_update", details=patch.model_dump(exclude_none=True))
    db.commit()
    return serialize_delivery(db, load_delivery(db, delivery.id))


@router.post("/bulk-update")
def bulk_update(
    payload: BulkUpdateRequest,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["supervisor"])
    ids = list(dict.fromkeys(payload.ids))
    if not ids:
        raise HTTPException(status_code=422, detail="No deliveries selected")
    deliveries = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(Delivery.id.in_(ids)).with_for_update().all()
    if len(deliveries) != len(ids):
        raise HTTPException(status_code=404, detail="One or more deliveries were not found")
    # Validate all against detached candidate values before mutating any row.
    for delivery in deliveries:
        data = payload.patch.model_dump(exclude_none=True)
        if "expectedPackages" in data:
            total = int(data["expectedPackages"])
            if total <= 0 or total > 999 or total < max(counter_map(delivery).values(), default=0):
                raise HTTPException(status_code=422, detail=f"Invalid places for {delivery.delivery_number}")
        if "status" in data:
            try:
                DeliveryStatus(data["status"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid status for {delivery.delivery_number}") from exc
    for delivery in deliveries:
        apply_patch(db, delivery, payload.patch)
        record_event(db, delivery, principal, "supervisor_bulk_update", details=payload.patch.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True, "updated": len(deliveries)}


def permanently_delete(db: Session, delivery: Delivery, principal: FirebasePrincipal, reason: Optional[str]) -> None:
    number = delivery.delivery_number
    db.query(DeliveryEvent).filter(DeliveryEvent.delivery_id == delivery.id).delete(synchronize_session=False)
    db.query(ScanEvent).filter(ScanEvent.delivery_id == delivery.id).delete(synchronize_session=False)
    db.query(ScanCounter).filter(ScanCounter.delivery_id == delivery.id).delete(synchronize_session=False)
    db.query(TransferOrder).filter(TransferOrder.delivery_id == delivery.id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.delivery_id == delivery.id).delete(synchronize_session=False)
    db.delete(delivery)
    db.add(DeliveryDeletionAudit(
        delivery_number=number,
        actor_uid=principal.uid,
        actor_name=principal.name,
        actor_role=principal.role,
        reason=reason,
    ))


@router.post("/bulk-delete")
def bulk_delete(
    payload: DeleteRequest,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["supervisor"])
    ids = list(dict.fromkeys(payload.ids))
    if not ids:
        raise HTTPException(status_code=422, detail="No deliveries selected")
    rows = db.query(Delivery).filter(Delivery.id.in_(ids)).with_for_update().all()
    if len(rows) != len(ids):
        raise HTTPException(status_code=404, detail="One or more deliveries were not found")
    numbers = [row.delivery_number for row in rows]
    for row in rows:
        permanently_delete(db, row, principal, payload.reason)
    db.commit()
    return {"ok": True, "deleted": len(rows), "deliveryNumbers": numbers}


@router.delete("/{number_or_id}")
def delete_one(
    number_or_id: str,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["supervisor"])
    delivery = load_delivery(db, number_or_id, for_update=True)
    number = delivery.delivery_number
    permanently_delete(db, delivery, principal, reason)
    db.commit()
    return {"ok": True, "deliveryNumber": number}


@router.post("/{number_or_id}/retry-enrichment")
def retry_enrichment(
    number_or_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    principal: FirebasePrincipal = Depends(get_firebase_principal),
):
    require_roles(principal, ["manager", "supervisor"])
    delivery = load_delivery(db, number_or_id)
    delivery.enrichment_status = "pending"
    delivery.enrichment_error = None
    db.commit()
    background_tasks.add_task(schedule_enrichment, delivery.id)
    return {"ok": True, "status": "pending"}
