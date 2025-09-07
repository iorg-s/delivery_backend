# app/models.py
import enum
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Enum, ForeignKey, JSON, func
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
