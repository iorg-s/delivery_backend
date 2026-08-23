# app/models.py
import enum
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Enum, ForeignKey, JSON, func,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from .db import Base
from sqlalchemy.orm import relationship


class UserRole(enum.Enum):
    supervisor = "supervisor"
    manager = "manager"
    driver = "driver"


class ScanStage(enum.Enum):
    source_pick = "source_pick"
    dest_arrival = "dest_arrival"
    dest_receive = "dest_receive"


class DeliveryStatus(enum.Enum):
    created = "created"
    # Kept because it exists in the original production PostgreSQL enum, even
    # though current flows move directly from created to partial_pick/picked.
    ready_to_pick = "ready_to_pick"
    partial_pick = "partial_pick"   # driver scanned some
    picked = "picked"               # driver scanned all
    in_transit = "in_transit"       # (optional, if you use it)
    arrived = "arrived"             # reached destination
    partial_receive = "partial_receive"  # manager scanned some
    received = "received"           # manager scanned all
    redirected = "redirected"



def gen_uuid():
    return str(uuid.uuid4())


class Warehouse(Base):
    __tablename__ = "warehouses"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    code = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    is_main = Column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    warehouse_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=True)
    warehouse = relationship("Warehouse")


class Delivery(Base):
    __tablename__ = "deliveries"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    delivery_number = Column(String, unique=True, nullable=False)
    source_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    destination_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    expected_packages = Column(Integer, nullable=False)
    status = Column(Enum(DeliveryStatus), nullable=False, default=DeliveryStatus.created)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    comment = Column(String, nullable=True)
    # V2 fields are additive so the original REST API and old Flutter app keep
    # working against the same rows during the staged rollout.
    delivery_type = Column(String, nullable=False, default="standard", server_default="standard")
    handover_status = Column(String, nullable=True)
    source_name_snapshot = Column(String, nullable=True)
    destination_name_snapshot = Column(String, nullable=True)
    current_location_name = Column(String, nullable=True)
    placement = Column(String, nullable=True)
    assigned_driver_uid = Column(String, nullable=True)
    assigned_driver_name = Column(String, nullable=True)
    arrived_at = Column(DateTime(timezone=True), nullable=True)
    arrived_by_uid = Column(String, nullable=True)
    arrived_by_name = Column(String, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    completed_by_uid = Column(String, nullable=True)
    completed_by_name = Column(String, nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)
    moysklad_document_type = Column(String, nullable=True)
    moysklad_document_id = Column(String, nullable=True)
    moysklad_document_name = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    item_summary = Column(JSON, nullable=True)
    enrichment_status = Column(String, nullable=False, default="not_requested", server_default="not_requested")
    enrichment_error = Column(String, nullable=True)
    enrichment_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    enriched_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    source = relationship("Warehouse", foreign_keys=[source_id])
    destination = relationship("Warehouse", foreign_keys=[destination_id])

    # 👇 add this relationship to scan counters
    scan_counters = relationship("ScanCounter", back_populates="delivery", cascade="all, delete-orphan")


class ScanEvent(Base):
    __tablename__ = "scan_events"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    delivery_id = Column(PGUUID(as_uuid=False), ForeignKey("deliveries.id"), nullable=False)
    stage = Column(Enum(ScanStage), nullable=False)
    scanned_by = Column(PGUUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    warehouse_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    client_device_id = Column(String, nullable=True)
    client_ts = Column(DateTime(timezone=True), nullable=True)


class ScanCounter(Base):
    __tablename__ = "scan_counters"
    delivery_id = Column(PGUUID(as_uuid=False), ForeignKey("deliveries.id"), primary_key=True)
    stage = Column(Enum(ScanStage), primary_key=True)
    total = Column(Integer, nullable=False, default=0)

    # 👇 link back to delivery
    delivery = relationship("Delivery", back_populates="scan_counters")


class DriverRoute(Base):
    __tablename__ = "driver_routes"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    driver_id = Column(PGUUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    route_date = Column(DateTime(timezone=False), nullable=False)
    warehouse_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)


class TransferOrder(Base):
    __tablename__ = "transfer_orders"
    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    delivery_id = Column(PGUUID(as_uuid=False), ForeignKey("deliveries.id"), nullable=False)
    from_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    to_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    expected_packages = Column(Integer, nullable=False)
    created_by = Column(PGUUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String, nullable=False, default="open")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(PGUUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    event_type = Column(String, nullable=False)
    delivery_id = Column(PGUUID(as_uuid=False), ForeignKey("deliveries.id"), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DeliveryEvent(Base):
    """V2 event stream that supports Firebase actors without legacy user links."""

    __tablename__ = "delivery_events"
    __table_args__ = (
        UniqueConstraint("actor_uid", "client_event_id", name="uq_delivery_event_actor_client"),
    )

    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    delivery_id = Column(PGUUID(as_uuid=False), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String, nullable=False)
    actor_uid = Column(String, nullable=False)
    actor_name = Column(String, nullable=False)
    actor_role = Column(String, nullable=False)
    warehouse_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=True)
    count = Column(Integer, nullable=False, default=0)
    client_event_id = Column(String, nullable=True)
    client_device_id = Column(String, nullable=True)
    client_ts = Column(DateTime(timezone=True), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FirebaseDriverRoute(Base):
    """Route selection keyed by Firebase UID; no email/user-table mapping required."""

    __tablename__ = "firebase_driver_routes"
    __table_args__ = (
        UniqueConstraint("actor_uid", "route_date", "warehouse_id", name="uq_firebase_driver_route"),
    )

    id = Column(PGUUID(as_uuid=False), primary_key=True, default=gen_uuid)
    actor_uid = Column(String, nullable=False, index=True)
    actor_name = Column(String, nullable=False)
    route_date = Column(DateTime(timezone=False), nullable=False, index=True)
    warehouse_id = Column(PGUUID(as_uuid=False), ForeignKey("warehouses.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DeliveryDeletionAudit(Base):
    """Minimal audit retained after an explicitly permanent deletion."""

    __tablename__ = "delivery_deletion_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    delivery_number = Column(String, nullable=False, index=True)
    actor_uid = Column(String, nullable=False)
    actor_name = Column(String, nullable=False)
    actor_role = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    deleted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
