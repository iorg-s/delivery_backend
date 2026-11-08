"""Two-way delivery bridge between this PostgreSQL backend and Firebase.

Firebase (`flutter_orders` Cloud Functions) polls ``GET /bridge/changes`` and
pushes its own scans/edits to ``POST /bridge/apply``. The shared key is the
delivery number. Counters are never copied over the other side's counters:
scan events are replayed through each side's own rules and recorded once.

Echo prevention: everything this module writes is recorded in
``delivery_events`` with ``actor_uid = "firebase-bridge"`` and is excluded
from ``/bridge/changes``. MoySklad is never called from here; only the system
where a scan really happened updates the move state.
"""
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..auth_v2 import FirebasePrincipal
from ..db import get_db
from ..models import (
    AuditLog,
    Delivery,
    DeliveryDeletionAudit,
    DeliveryEvent,
    DeliveryStatus,
    ScanCounter,
    ScanEvent,
    ScanStage,
    User,
    Warehouse,
)
from .v2.deliveries import permanently_delete, status_value

BRIDGE_ACTOR_UID = "firebase-bridge"
BRIDGE_ACTOR_NAME = "Firebase bridge"
# Re-read this far behind the cursor: PostgreSQL stamps rows with the
# transaction start time, so a slow transaction can commit "in the past".
# Replays are harmless because Firebase applies each event id only once.
OVERLAP = timedelta(minutes=5)
MAX_EVENTS = 1000
EVENT_IDS_PER_DELIVERY = 200
EXPORTED_V2_EVENTS = {"pickup": "pickup", "arrival_main": "arrival", "receive": "receive"}
STAGE_ACTIONS = {
    ScanStage.source_pick: "pickup",
    ScanStage.dest_arrival: "arrival",
    ScanStage.dest_receive: "receive",
}
BRIDGE_PRINCIPAL = FirebasePrincipal(
    uid=BRIDGE_ACTOR_UID,
    name=BRIDGE_ACTOR_NAME,
    role="supervisor",
    warehouse_id=None,
    warehouse_name="",
)


def require_bridge_secret(x_bridge_secret: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("DELIVERY_BRIDGE_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Bridge is not configured")
    if not hmac.compare_digest((x_bridge_secret or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid bridge secret")


router = APIRouter(prefix="/bridge", tags=["bridge"], dependencies=[Depends(require_bridge_secret)])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    value = as_utc(value)
    return value.isoformat() if value else None


def parse_cursor(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


def warehouse_match_key(value: Any) -> str:
    """Python twin of ``warehouseMatchKey`` in functions/src/delivery-v2-core.ts."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = re.sub(r"^(магазин|magazin|shop|склад)\s+", "", text)
    return re.sub(r"[\W_]+", " ", text).strip()


def is_done(delivery: Delivery) -> bool:
    return delivery.archived_at is not None or status_value(delivery.status) == DeliveryStatus.received.value


def counters_of(delivery: Delivery) -> Dict[str, int]:
    return {status_value(row.stage): int(row.total or 0) for row in (delivery.scan_counters or [])}


def database_now(db: Session) -> datetime:
    if db.get_bind().dialect.name == "postgresql":
        return as_utc(db.execute(select(func.now())).scalar_one())
    return utcnow()


def consistent_snapshot(db: Session) -> None:
    """Read deliveries, counters and events from one PostgreSQL snapshot."""
    if db.get_bind().dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def warehouse_json(row: Optional[Warehouse]) -> Dict[str, Any]:
    if row is None:
        return {"id": None, "name": "", "isMain": False}
    return {"id": row.id, "name": row.name, "isMain": bool(row.is_main)}


def delivery_json(delivery: Delivery, warehouses: Dict[str, Warehouse], event_ids: List[str]) -> Dict[str, Any]:
    source = warehouses.get(str(delivery.source_id))
    destination = warehouses.get(str(delivery.destination_id))
    return {
        "renderId": delivery.id,
        "deliveryNumber": delivery.delivery_number,
        "type": delivery.delivery_type or "standard",
        "handoverStatus": delivery.handover_status,
        "status": status_value(delivery.status),
        "done": is_done(delivery),
        "expectedPackages": int(delivery.expected_packages or 0),
        "source": warehouse_json(source),
        "destination": warehouse_json(destination),
        "sourceName": delivery.source_name_snapshot or (source.name if source else ""),
        "destinationName": delivery.destination_name_snapshot or (destination.name if destination else ""),
        "comment": delivery.comment or "",
        "collectorName": delivery.collector_name or "",
        "placement": delivery.placement or "",
        "counters": counters_of(delivery),
        "currentLocationName": delivery.current_location_name or "",
        "assignedDriverUid": delivery.assigned_driver_uid or "",
        "assignedDriverName": delivery.assigned_driver_name or "",
        "completedAt": iso(delivery.completed_at),
        "completedByName": delivery.completed_by_name or "",
        "archivedAt": iso(delivery.archived_at),
        "createdAt": iso(delivery.created_at),
        "updatedAt": iso(delivery.updated_at),
        "eventIds": event_ids[:EVENT_IDS_PER_DELIVERY],
    }


def scan_event_json(row: ScanEvent, number: str, user: Optional[User]) -> Dict[str, Any]:
    email = str(user.email or "") if user else ""
    return {
        "id": row.id,
        "table": "scan_events",
        "deliveryNumber": number,
        "action": STAGE_ACTIONS[row.stage if isinstance(row.stage, ScanStage) else ScanStage(row.stage)],
        "count": int(row.count or 0),
        "actorUid": f"legacy:{row.scanned_by}",
        "actorName": email.split("@", 1)[0] if email else str(row.scanned_by),
        "actorRole": status_value(user.role) if user else "",
        "deviceId": row.client_device_id or "",
        "createdAt": iso(row.created_at),
    }


def delivery_event_json(row: DeliveryEvent, number: str) -> Dict[str, Any]:
    return {
        "id": row.id,
        "table": "delivery_events",
        "deliveryNumber": number,
        "action": EXPORTED_V2_EVENTS[row.event_type],
        "count": int(row.count or 0),
        "actorUid": row.actor_uid,
        "actorName": row.actor_name,
        "actorRole": row.actor_role,
        "deviceId": row.client_device_id or "",
        "createdAt": iso(row.created_at),
    }


def window_events(db: Session, lower: datetime, upper: datetime, delivery_ids=None, limit: Optional[int] = None):
    """Scan events of both generations, oldest first, minus bridge-applied ones."""
    scans = db.query(ScanEvent, Delivery.delivery_number, User).join(
        Delivery, Delivery.id == ScanEvent.delivery_id,
    ).outerjoin(User, User.id == ScanEvent.scanned_by).filter(
        ScanEvent.created_at >= lower, ScanEvent.created_at <= upper,
    )
    v2 = db.query(DeliveryEvent, Delivery.delivery_number).join(
        Delivery, Delivery.id == DeliveryEvent.delivery_id,
    ).filter(
        DeliveryEvent.created_at >= lower,
        DeliveryEvent.created_at <= upper,
        DeliveryEvent.actor_uid != BRIDGE_ACTOR_UID,
        DeliveryEvent.event_type.in_(list(EXPORTED_V2_EVENTS)),
    )
    if delivery_ids is not None:
        scans = scans.filter(ScanEvent.delivery_id.in_(list(delivery_ids)))
        v2 = v2.filter(DeliveryEvent.delivery_id.in_(list(delivery_ids)))
    if limit:
        scans = scans.order_by(ScanEvent.created_at.asc()).limit(limit + 1)
        v2 = v2.order_by(DeliveryEvent.created_at.asc()).limit(limit + 1)
    rows = [(as_utc(row.created_at), row.delivery_id, scan_event_json(row, number, user)) for row, number, user in scans.all()]
    rows += [(as_utc(row.created_at), row.delivery_id, delivery_event_json(row, number)) for row, number in v2.all()]
    rows.sort(key=lambda item: (item[0] or utcnow(), item[2]["id"]))
    return rows


def deleted_numbers(db: Session, lower: datetime, upper: datetime) -> List[Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in db.query(DeliveryDeletionAudit).filter(
        DeliveryDeletionAudit.deleted_at >= lower,
        DeliveryDeletionAudit.deleted_at <= upper,
        DeliveryDeletionAudit.actor_uid != BRIDGE_ACTOR_UID,
    ).all():
        result[row.delivery_number] = {
            "deliveryNumber": row.delivery_number,
            "deletedAt": iso(row.deleted_at),
            "actorName": row.actor_name,
            "reason": row.reason or "",
        }
    for row in db.query(AuditLog).filter(
        AuditLog.event_type == "delivery_deleted",
        AuditLog.created_at >= lower,
        AuditLog.created_at <= upper,
    ).all():
        number = str((row.details or {}).get("delivery_number") or "").strip()
        if number:
            result.setdefault(number, {
                "deliveryNumber": number, "deletedAt": iso(row.created_at), "actorName": "", "reason": "",
            })
    if not result:
        return []
    # A number deleted and then created again is not a deletion any more.
    alive = {row[0] for row in db.query(Delivery.delivery_number).filter(Delivery.delivery_number.in_(list(result))).all()}
    return [value for number, value in result.items() if number not in alive]


@router.get("/changes")
def bridge_changes(
    since: Optional[str] = None,
    full: bool = False,
    db: Session = Depends(get_db),
):
    """Deliveries, scan events and deletions since ``since``.

    ``full=true`` (or no cursor) returns every active delivery with its
    current counters and no events: the one-time seed. Each delivery lists
    the ids of its recent events (``eventIds``); a side that creates the
    delivery from this snapshot marks those as already counted.
    """
    consistent_snapshot(db)
    now = database_now(db)
    cursor = parse_cursor(since)
    warehouses = {str(row.id): row for row in db.query(Warehouse).all()}
    full = full or cursor is None

    if full:
        events: List[Dict[str, Any]] = []
        next_cursor = now
        deliveries = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(
            Delivery.archived_at.is_(None),
            Delivery.status != DeliveryStatus.received,
        ).all()
        deletions: List[Dict[str, Any]] = []
        id_window = window_events(db, now - OVERLAP, now, {row.id for row in deliveries}) if deliveries else []
        has_more = False
    else:
        lower = cursor - OVERLAP
        rows = window_events(db, lower, now, limit=MAX_EVENTS)
        has_more = len(rows) > MAX_EVENTS
        rows = rows[:MAX_EVENTS]
        next_cursor = rows[-1][0] if has_more else now
        events = [item[2] for item in rows]
        event_delivery_ids = {item[1] for item in rows}
        query = db.query(Delivery).options(selectinload(Delivery.scan_counters))
        updated = query.filter(Delivery.updated_at >= lower, Delivery.updated_at <= next_cursor).all()
        by_id = {row.id: row for row in updated}
        if event_delivery_ids - set(by_id):
            for row in query.filter(Delivery.id.in_(list(event_delivery_ids - set(by_id)))).all():
                by_id[row.id] = row
        deliveries = list(by_id.values())
        deletions = deleted_numbers(db, lower, next_cursor)
        id_window = rows

    ids_by_delivery: Dict[str, List[str]] = {}
    for _, delivery_id, event in id_window:
        ids_by_delivery.setdefault(delivery_id, []).append(event["id"])

    return {
        "cursor": iso(next_cursor),
        "serverTime": iso(now),
        "full": full,
        "hasMore": has_more,
        "warehouses": [
            {"id": row.id, "code": row.code, "name": row.name, "isMain": bool(row.is_main)}
            for row in warehouses.values()
        ],
        "deliveries": [delivery_json(row, warehouses, ids_by_delivery.get(row.id, [])) for row in deliveries],
        "events": events,
        "deletions": deletions,
    }


class BridgeLookupRequest(BaseModel):
    numbers: List[str] = Field(default_factory=list)


class ArchiveStaleIn(BaseModel):
    pickedOlderThanDays: int = 7
    dryRun: bool = False


@router.post("/archive_stale")
def bridge_archive_stale(payload: ArchiveStaleIn, db: Session = Depends(get_db)):
    """Close deliveries picked long ago that no shop ever scanned as received.

    They are marked received (the legacy driver app hides only received rows)
    and archived, signed as an automatic close. Their updated_at changes, so
    the next /bridge/changes carries them to Firebase, which archives them too.
    """
    days = max(1, min(365, int(payload.pickedOlderThanDays or 7)))
    cutoff = database_now(db) - timedelta(days=days)
    rows = db.query(Delivery).filter(
        Delivery.archived_at.is_(None),
        Delivery.status == DeliveryStatus.picked,
        Delivery.updated_at < cutoff,
    ).with_for_update().all()
    numbers = [row.delivery_number for row in rows]
    if not payload.dryRun and rows:
        now = utcnow()
        for row in rows:
            row.status = DeliveryStatus.received
            row.archived_at = now
            row.completed_at = now
            row.completed_by_uid = BRIDGE_ACTOR_UID
            row.completed_by_name = f"Авто: забрано более {days} дн. назад"
            bridge_event(db, row, "auto_archived", None,
                         details={"reason": "picked_older_than_days", "days": days})
        db.commit()
    return {"archived": len(numbers), "numbers": numbers[:200], "days": days, "dryRun": payload.dryRun}


@router.post("/lookup")
def bridge_lookup(payload: BridgeLookupRequest, db: Session = Depends(get_db)):
    """State of specific numbers: used by the seed for Firebase-only active rows."""
    numbers = list(dict.fromkeys(str(value).strip() for value in payload.numbers if str(value).strip()))[:1000]
    if not numbers:
        return {"rows": []}
    warehouses = {str(row.id): row for row in db.query(Warehouse).all()}
    found = {
        row.delivery_number: row
        for row in db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(
            Delivery.delivery_number.in_(numbers),
        ).all()
    }
    deleted = {
        row[0] for row in db.query(DeliveryDeletionAudit.delivery_number).filter(
            DeliveryDeletionAudit.delivery_number.in_(numbers),
        ).all()
    }
    rows = []
    for number in numbers:
        delivery = found.get(number)
        if delivery is not None:
            rows.append({"deliveryNumber": number, "state": "done" if is_done(delivery) else "active",
                         "delivery": delivery_json(delivery, warehouses, [])})
        else:
            rows.append({"deliveryNumber": number, "state": "deleted" if number in deleted else "missing"})
    return {"rows": rows}


class BridgeDeliveryIn(BaseModel):
    deliveryNumber: str
    type: str = "standard"
    expectedPackages: int = 1
    sourceId: Optional[str] = None
    sourceName: Optional[str] = None
    sourceIsMain: bool = False
    destinationId: Optional[str] = None
    destinationName: Optional[str] = None
    destinationIsMain: bool = False
    comment: Optional[str] = None
    placement: Optional[str] = None
    # Used only when the delivery does not exist here yet.
    status: Optional[str] = None
    counters: Dict[str, int] = Field(default_factory=dict)
    includedEventIds: List[str] = Field(default_factory=list)


class BridgeEventIn(BaseModel):
    id: str
    deliveryNumber: str
    action: Literal["pickup", "arrival", "receive", "complete", "delete"]
    count: int = 0
    actorUid: str = ""
    actorName: str = ""
    actorRole: str = ""
    deviceId: Optional[str] = None
    reason: Optional[str] = None


class BridgeApplyRequest(BaseModel):
    deliveries: List[BridgeDeliveryIn] = Field(default_factory=list)
    events: List[BridgeEventIn] = Field(default_factory=list)


def resolve_warehouse(db: Session, warehouse_id: Optional[str], name: Optional[str], is_main: bool) -> Optional[Warehouse]:
    if warehouse_id:
        row = db.query(Warehouse).filter(Warehouse.id == warehouse_id).one_or_none()
        if row is not None:
            return row
    rows = db.query(Warehouse).all()
    if is_main:
        main = next((row for row in rows if row.is_main), None)
        if main is not None:
            return main
    key = warehouse_match_key(name)
    if not key:
        return None
    return next((row for row in rows if warehouse_match_key(row.name) == key), None)


def bridge_status(value: Optional[str]) -> DeliveryStatus:
    text = str(value or "").strip()
    if text in {"completed", "cancelled", "broderie_back"}:
        return DeliveryStatus.received
    try:
        return DeliveryStatus(text)
    except ValueError:
        return DeliveryStatus.created


def bridge_event(
    db: Session,
    delivery: Delivery,
    event_type: str,
    client_event_id: Optional[str],
    count: int = 0,
    warehouse_id: Optional[str] = None,
    actor_name: str = BRIDGE_ACTOR_NAME,
    actor_role: str = "supervisor",
    device_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> DeliveryEvent:
    row = DeliveryEvent(
        delivery_id=delivery.id,
        event_type=event_type,
        actor_uid=BRIDGE_ACTOR_UID,
        actor_name=actor_name or BRIDGE_ACTOR_NAME,
        actor_role=actor_role or "driver",
        warehouse_id=warehouse_id,
        count=count,
        client_event_id=client_event_id,
        client_device_id=device_id,
        details={"origin": "firebase", **(details or {})},
    )
    db.add(row)
    return row


def upsert_delivery(db: Session, payload: BridgeDeliveryIn) -> Dict[str, Any]:
    number = payload.deliveryNumber.strip()
    if not number:
        return {"deliveryNumber": number, "result": "error", "detail": "Empty delivery number"}
    source = resolve_warehouse(db, payload.sourceId, payload.sourceName, payload.sourceIsMain)
    destination = resolve_warehouse(db, payload.destinationId, payload.destinationName, payload.destinationIsMain)
    delivery = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(
        Delivery.delivery_number == number,
    ).with_for_update().one_or_none()
    expected = max(1, min(999, int(payload.expectedPackages or 1)))

    if delivery is None:
        if source is None or destination is None:
            return {"deliveryNumber": number, "result": "conflict", "detail": "Unknown source or destination warehouse"}
        status = bridge_status(payload.status)
        now = utcnow()
        delivery = Delivery(
            delivery_number=number,
            source_id=source.id,
            destination_id=destination.id,
            expected_packages=expected,
            status=status,
            comment=payload.comment or None,
            placement=payload.placement or None,
            delivery_type=(payload.type or "standard").strip() or "standard",
            source_name_snapshot=source.name,
            destination_name_snapshot=destination.name,
            current_location_name=source.name,
            enrichment_status="not_requested",
        )
        if status == DeliveryStatus.received:
            delivery.archived_at = now
            delivery.completed_at = now
            delivery.completed_by_uid = BRIDGE_ACTOR_UID
            delivery.completed_by_name = BRIDGE_ACTOR_NAME
        db.add(delivery)
        db.flush()
        for stage in ScanStage:
            total = max(0, min(expected, int(payload.counters.get(stage.value, 0) or 0)))
            if total:
                db.add(ScanCounter(delivery_id=delivery.id, stage=stage, total=total))
        bridge_event(db, delivery, "bridge_created", None, details={"counters": payload.counters})
        # The counters above already include these Firebase events; recording
        # their ids makes a later push of the same events a duplicate.
        for event_id in dict.fromkeys(payload.includedEventIds):
            bridge_event(db, delivery, "bridge_included", event_id)
        return {"deliveryNumber": number, "result": "created"}

    if is_done(delivery):
        return {"deliveryNumber": number, "result": "unchanged", "detail": "Delivery is already completed"}
    changed: Dict[str, Any] = {}
    conflicts: List[str] = []
    scanned_max = max(counters_of(delivery).values(), default=0)
    if expected != delivery.expected_packages:
        if expected < scanned_max:
            conflicts.append(f"expectedPackages {expected} < scanned {scanned_max}")
        else:
            delivery.expected_packages = expected
            changed["expectedPackages"] = expected
    if source is not None and str(source.id) != str(delivery.source_id):
        delivery.source_id = source.id
        delivery.source_name_snapshot = source.name
        changed["sourceId"] = source.id
    if destination is not None and str(destination.id) != str(delivery.destination_id):
        delivery.destination_id = destination.id
        delivery.destination_name_snapshot = destination.name
        changed["destinationId"] = destination.id
    for field, column in (("comment", "comment"), ("placement", "placement")):
        value = getattr(payload, field) or None
        if value != (getattr(delivery, column) or None):
            setattr(delivery, column, value)
            changed[field] = value
    if (source is None and payload.sourceName) or (destination is None and payload.destinationName):
        conflicts.append("Unknown source or destination warehouse")
    if changed:
        delivery.version = int(delivery.version or 1) + 1
        bridge_event(db, delivery, "bridge_update", None, details=changed)
    result = "updated" if changed else "unchanged"
    if conflicts:
        return {"deliveryNumber": number, "result": "conflict" if not changed else result, "detail": "; ".join(conflicts)}
    return {"deliveryNumber": number, "result": result}


def counter_row(db: Session, delivery: Delivery, stage: ScanStage) -> ScanCounter:
    row = db.query(ScanCounter).filter(
        ScanCounter.delivery_id == delivery.id, ScanCounter.stage == stage,
    ).with_for_update().one_or_none()
    if row is None:
        row = ScanCounter(delivery_id=delivery.id, stage=stage, total=0)
        db.add(row)
        db.flush()
    return row


def apply_event(db: Session, payload: BridgeEventIn) -> Dict[str, Any]:
    """Apply one Firebase event with the v1 ``/deliveries/scan`` rules, minus MoySklad."""
    result: Dict[str, Any] = {"id": payload.id}
    duplicate = db.query(DeliveryEvent.id).filter(
        DeliveryEvent.actor_uid == BRIDGE_ACTOR_UID,
        DeliveryEvent.client_event_id == payload.id,
    ).first()
    if duplicate:
        return {**result, "result": "duplicate"}
    delivery = db.query(Delivery).options(selectinload(Delivery.scan_counters)).filter(
        Delivery.delivery_number == payload.deliveryNumber.strip(),
    ).with_for_update().one_or_none()
    if delivery is None:
        return {**result, "result": "not_found"}

    actor = {
        "actor_name": payload.actorName or BRIDGE_ACTOR_NAME,
        "actor_role": payload.actorRole or "driver",
        "device_id": payload.deviceId,
    }
    origin = {"firebaseActorUid": payload.actorUid, "requestedCount": payload.count}
    now = utcnow()

    if payload.action == "delete":
        permanently_delete(db, delivery, BRIDGE_PRINCIPAL, payload.reason or "Deleted in Firebase")
        return {**result, "result": "applied"}

    if payload.action == "complete":
        if not is_done(delivery):
            delivery.status = DeliveryStatus.received
            delivery.completed_at = now
            delivery.completed_by_uid = BRIDGE_ACTOR_UID
            delivery.completed_by_name = payload.actorName or BRIDGE_ACTOR_NAME
            delivery.archived_at = now
            delivery.version = int(delivery.version or 1) + 1
        bridge_event(db, delivery, "supervisor_complete", payload.id, details={**origin, "reason": payload.reason}, **actor)
        return {**result, "result": "applied"}

    stage = {"pickup": ScanStage.source_pick, "arrival": ScanStage.dest_arrival, "receive": ScanStage.dest_receive}[payload.action]
    event_type = {"pickup": "pickup", "arrival": "arrival_main", "receive": "receive"}[payload.action]
    conflict = ""
    if is_done(delivery):
        conflict = "Delivery is already completed"
    elif stage == ScanStage.dest_arrival and delivery.status != DeliveryStatus.picked:
        conflict = "Delivery must be fully picked before arrival scan"
    elif stage == ScanStage.dest_receive and delivery.status not in (
        DeliveryStatus.picked, DeliveryStatus.arrived, DeliveryStatus.partial_receive,
    ):
        conflict = "Delivery must be picked/arrived first"

    increment = 0
    if not conflict:
        counter = counter_row(db, delivery, stage)
        new_total = min(int(counter.total or 0) + max(0, payload.count), delivery.expected_packages)
        increment = max(0, new_total - int(counter.total or 0))
        if increment < payload.count:
            conflict = f"Capped at expected places: applied {increment} of {payload.count}"
        counter.total = new_total
        if increment > 0:
            if stage == ScanStage.source_pick:
                delivery.assigned_driver_uid = payload.actorUid or delivery.assigned_driver_uid
                delivery.assigned_driver_name = payload.actorName or delivery.assigned_driver_name
                if new_total < delivery.expected_packages:
                    delivery.status = DeliveryStatus.partial_pick
                else:
                    delivery.status = DeliveryStatus.picked
                    delivery.current_location_name = f"У водителя: {payload.actorName}" if payload.actorName else delivery.current_location_name
            elif stage == ScanStage.dest_arrival:
                delivery.status = DeliveryStatus.arrived
                if delivery.arrived_at is None:
                    delivery.arrived_at = now
                    delivery.arrived_by_uid = payload.actorUid or BRIDGE_ACTOR_UID
                    delivery.arrived_by_name = payload.actorName or BRIDGE_ACTOR_NAME
                delivery.current_location_name = delivery.destination.name if delivery.destination else None
            else:
                delivery.current_location_name = delivery.destination.name if delivery.destination else None
                if new_total >= delivery.expected_packages:
                    delivery.status = DeliveryStatus.received
                    delivery.completed_at = now
                    delivery.completed_by_uid = payload.actorUid or BRIDGE_ACTOR_UID
                    delivery.completed_by_name = payload.actorName or BRIDGE_ACTOR_NAME
                    delivery.archived_at = now
                else:
                    delivery.status = DeliveryStatus.partial_receive
            delivery.version = int(delivery.version or 1) + 1
            db.add(AuditLog(
                actor_id=None,
                event_type="scan",
                delivery_id=delivery.id,
                details={"stage": stage.value, "count": increment, "source": BRIDGE_ACTOR_UID, "actor": payload.actorName},
            ))

    warehouse_id = delivery.source_id if stage == ScanStage.source_pick else delivery.destination_id
    bridge_event(
        db, delivery, event_type, payload.id, count=increment, warehouse_id=warehouse_id,
        details={**origin, **({"conflict": conflict} if conflict else {})}, **actor,
    )
    db.flush()
    db.refresh(delivery)
    return {
        **result,
        "result": "conflict" if conflict else "applied",
        "applied": increment,
        **({"detail": conflict} if conflict else {}),
        "delivery": {"status": status_value(delivery.status), "done": is_done(delivery), "counters": counters_of(delivery)},
    }


@router.post("/apply")
def bridge_apply(payload: BridgeApplyRequest, db: Session = Depends(get_db)):
    """Upsert Firebase deliveries, then apply Firebase events, each atomically."""
    delivery_results = []
    for item in payload.deliveries:
        try:
            delivery_results.append(upsert_delivery(db, item))
            db.commit()
        except IntegrityError:
            db.rollback()
            delivery_results.append({"deliveryNumber": item.deliveryNumber, "result": "retry", "detail": "Concurrent write"})
        except Exception as exc:  # one bad row must not block the rest
            db.rollback()
            delivery_results.append({"deliveryNumber": item.deliveryNumber, "result": "error", "detail": str(exc)[:300]})
    event_results = []
    for item in payload.events:
        try:
            event_results.append(apply_event(db, item))
            db.commit()
        except IntegrityError:
            # uq_delivery_event_actor_client: the same event raced in twice.
            db.rollback()
            event_results.append({"id": item.id, "result": "duplicate"})
        except Exception as exc:
            db.rollback()
            event_results.append({"id": item.id, "result": "error", "detail": str(exc)[:300]})
    return {"deliveries": delivery_results, "events": event_results}
