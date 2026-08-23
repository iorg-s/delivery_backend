"""delivery v2 foundation

Revision ID: 6f42c1e9d310
Revises: 17301793ba4f
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f42c1e9d310"
down_revision: Union[str, Sequence[str], None] = "17301793ba4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older production databases were created before partial scan statuses were
    # added to the ORM enum. IF NOT EXISTS keeps the migration rerunnable.
    op.execute("ALTER TYPE deliverystatus ADD VALUE IF NOT EXISTS 'partial_pick'")
    op.execute("ALTER TYPE deliverystatus ADD VALUE IF NOT EXISTS 'partial_receive'")
    op.add_column("deliveries", sa.Column("delivery_type", sa.String(), nullable=False, server_default="standard"))
    op.add_column("deliveries", sa.Column("handover_status", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("source_name_snapshot", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("destination_name_snapshot", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("current_location_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("placement", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("assigned_driver_uid", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("assigned_driver_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("arrived_by_uid", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("arrived_by_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("completed_by_uid", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("completed_by_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("moysklad_document_type", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("moysklad_document_id", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("moysklad_document_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("company_name", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("item_summary", sa.JSON(), nullable=True))
    op.add_column("deliveries", sa.Column("enrichment_status", sa.String(), nullable=False, server_default="not_requested"))
    op.add_column("deliveries", sa.Column("enrichment_error", sa.String(), nullable=True))
    op.add_column("deliveries", sa.Column("enrichment_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("deliveries", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_deliveries_archived_at", "deliveries", ["archived_at"])
    # Existing legacy completions must start in V2 history, not reappear as
    # active work. Future V1 completions also set these fields in the API.
    op.execute("""
        UPDATE deliveries
        SET archived_at = COALESCE(updated_at, created_at, now()),
            completed_at = COALESCE(updated_at, created_at, now())
        WHERE status = 'received' AND archived_at IS NULL
    """)

    op.create_table(
        "delivery_events",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("delivery_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_uid", sa.String(), nullable=False),
        sa.Column("actor_name", sa.String(), nullable=False),
        sa.Column("actor_role", sa.String(), nullable=False),
        sa.Column("warehouse_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("client_event_id", sa.String(), nullable=True),
        sa.Column("client_device_id", sa.String(), nullable=True),
        sa.Column("client_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_uid", "client_event_id", name="uq_delivery_event_actor_client"),
    )

    op.create_table(
        "firebase_driver_routes",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("actor_uid", sa.String(), nullable=False),
        sa.Column("actor_name", sa.String(), nullable=False),
        sa.Column("route_date", sa.DateTime(), nullable=False),
        sa.Column("warehouse_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_uid", "route_date", "warehouse_id", name="uq_firebase_driver_route"),
    )
    op.create_index("ix_firebase_driver_routes_actor_uid", "firebase_driver_routes", ["actor_uid"])
    op.create_index("ix_firebase_driver_routes_route_date", "firebase_driver_routes", ["route_date"])

    op.create_table(
        "delivery_deletion_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_number", sa.String(), nullable=False),
        sa.Column("actor_uid", sa.String(), nullable=False),
        sa.Column("actor_name", sa.String(), nullable=False),
        sa.Column("actor_role", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delivery_deletion_audit_delivery_number", "delivery_deletion_audit", ["delivery_number"])


def downgrade() -> None:
    op.drop_index("ix_delivery_deletion_audit_delivery_number", table_name="delivery_deletion_audit")
    op.drop_table("delivery_deletion_audit")
    op.drop_index("ix_firebase_driver_routes_route_date", table_name="firebase_driver_routes")
    op.drop_index("ix_firebase_driver_routes_actor_uid", table_name="firebase_driver_routes")
    op.drop_table("firebase_driver_routes")
    op.drop_table("delivery_events")
    op.drop_index("ix_deliveries_archived_at", table_name="deliveries")
    for name in [
        "version", "enriched_at", "enrichment_attempts", "enrichment_error",
        "enrichment_status", "item_summary", "company_name", "moysklad_document_name",
        "moysklad_document_id", "moysklad_document_type", "archived_at",
        "completed_by_name", "completed_by_uid", "completed_at", "arrived_by_name",
        "arrived_by_uid", "arrived_at", "assigned_driver_name", "assigned_driver_uid",
        "placement", "current_location_name", "destination_name_snapshot",
        "source_name_snapshot", "handover_status", "delivery_type",
    ]:
        op.drop_column("deliveries", name)
