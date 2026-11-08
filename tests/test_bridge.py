import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import bridge
from app.db import Base, get_db
from app.models import (
    AuditLog, Delivery, DeliveryDeletionAudit, DeliveryEvent, DeliveryStatus,
    ScanCounter, ScanEvent, ScanStage, User, UserRole, Warehouse,
)

@compiles(PGUUID, "sqlite")
def _uuid_as_text(type_, compiler, **kw):
    # A column named UUID gets NUMERIC affinity in SQLite and digit-only ids
    # come back as integers; PostgreSQL itself is unaffected.
    return "CHAR(36)"


SECRET = "test-bridge-secret"
HEADERS = {"X-Bridge-Secret": SECRET}


def utc(minutes_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        app = FastAPI()
        app.include_router(bridge.router)

        def override():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        self.client = TestClient(app)
        self.env = mock.patch.dict(os.environ, {"DELIVERY_BRIDGE_SECRET": SECRET})
        self.env.start()
        self.moysklad = mock.patch("app.api.v1.endpoints.notify_moysklad")
        self.notify = self.moysklad.start()

        db = self.Session()
        db.add_all([
            Warehouse(id="00000000-0000-0000-0000-000000000001", code="MAIN", name="Основной склад", is_main=True),
            Warehouse(id="00000000-0000-0000-0000-000000000002", code="BAL", name="Balti", is_main=False),
            Warehouse(id="00000000-0000-0000-0000-000000000003", code="CEN", name="Магазин Централка", is_main=False),
            User(id="00000000-0000-0000-0000-0000000000aa", email="ion@example.com", password_hash="x",
                 role=UserRole.driver),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        self.moysklad.stop()
        self.env.stop()

    def add_delivery(self, number="MOVE-1", expected=3, picked=0, status=DeliveryStatus.created, minutes_ago=0):
        db = self.Session()
        delivery = Delivery(
            delivery_number=number,
            source_id="00000000-0000-0000-0000-000000000001",
            destination_id="00000000-0000-0000-0000-000000000002",
            expected_packages=expected,
            status=status,
            updated_at=utc(minutes_ago),
        )
        db.add(delivery)
        db.flush()
        if picked:
            db.add(ScanCounter(delivery_id=delivery.id, stage=ScanStage.source_pick, total=picked))
        db.commit()
        delivery_id = delivery.id
        db.close()
        return delivery_id

    def counters(self, number="MOVE-1"):
        db = self.Session()
        delivery = db.query(Delivery).filter(Delivery.delivery_number == number).one()
        result = ({bridge.status_value(row.stage): row.total for row in delivery.scan_counters},
                  bridge.status_value(delivery.status), delivery.archived_at is not None)
        db.close()
        return result

    def apply(self, deliveries=(), events=()):
        response = self.client.post("/bridge/apply", headers=HEADERS,
                                    json={"deliveries": list(deliveries), "events": list(events)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class SecretTests(BridgeTestCase):
    def test_missing_or_wrong_secret_is_rejected(self):
        self.assertEqual(self.client.get("/bridge/changes").status_code, 401)
        self.assertEqual(self.client.get("/bridge/changes", headers={"X-Bridge-Secret": "nope"}).status_code, 401)
        self.assertEqual(self.client.get("/bridge/changes", headers=HEADERS).status_code, 200)

    def test_unconfigured_secret_disables_the_bridge(self):
        with mock.patch.dict(os.environ, {"DELIVERY_BRIDGE_SECRET": ""}):
            self.assertEqual(self.client.get("/bridge/changes", headers=HEADERS).status_code, 503)

    def test_main_app_includes_bridge(self):
        from app.main import app
        self.assertIn("/bridge/apply", {route.path for route in app.routes})


class ApplyEventTests(BridgeTestCase):
    def pickup(self, event_id="fb-1", count=1, number="MOVE-1"):
        return {"id": event_id, "deliveryNumber": number, "action": "pickup", "count": count,
                "actorUid": "uid-1", "actorName": "Driver One", "actorRole": "driver"}

    def test_pickup_counts_once_and_skips_moysklad(self):
        self.add_delivery(expected=2)
        first = self.apply(events=[self.pickup("fb-1"), self.pickup("fb-2")])
        self.assertEqual([row["result"] for row in first["events"]], ["applied", "applied"])
        replay = self.apply(events=[self.pickup("fb-1"), self.pickup("fb-2")])
        self.assertEqual([row["result"] for row in replay["events"]], ["duplicate", "duplicate"])
        counters, status, done = self.counters()
        self.assertEqual((counters["source_pick"], status, done), (2, "picked", False))
        self.notify.assert_not_called()
        db = self.Session()
        rows = db.query(DeliveryEvent).filter(DeliveryEvent.client_event_id.in_(["fb-1", "fb-2"])).all()
        self.assertEqual({row.actor_uid for row in rows}, {"firebase-bridge"})
        db.close()

    def test_over_scan_is_capped_like_v1_and_reported(self):
        self.add_delivery(expected=2, picked=1, status=DeliveryStatus.partial_pick)
        result = self.apply(events=[self.pickup("fb-9", count=3)])["events"][0]
        self.assertEqual(result["result"], "conflict")
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.counters()[0]["source_pick"], 2)
        # A capped event is still recorded, so its replay is a duplicate.
        self.assertEqual(self.apply(events=[self.pickup("fb-9", count=3)])["events"][0]["result"], "duplicate")

    def test_full_receive_archives_without_moysklad(self):
        self.add_delivery(expected=2, picked=2, status=DeliveryStatus.picked)
        result = self.apply(events=[{"id": "fb-r", "deliveryNumber": "MOVE-1", "action": "receive", "count": 2,
                                     "actorName": "Manager", "actorRole": "manager"}])["events"][0]
        self.assertEqual(result["result"], "applied")
        counters, status, done = self.counters()
        self.assertEqual((counters["dest_receive"], status, done), (2, "received", True))
        self.notify.assert_not_called()

    def test_receive_before_pickup_is_a_conflict_without_counting(self):
        self.add_delivery(expected=2)
        result = self.apply(events=[{"id": "fb-r", "deliveryNumber": "MOVE-1", "action": "receive", "count": 1}])
        self.assertEqual(result["events"][0]["result"], "conflict")
        self.assertEqual(self.counters()[0].get("dest_receive", 0), 0)

    def test_unknown_delivery_is_not_found(self):
        self.assertEqual(self.apply(events=[self.pickup(number="NOPE")])["events"][0]["result"], "not_found")

    def test_complete_and_delete(self):
        self.add_delivery("MOVE-C")
        self.add_delivery("MOVE-D")
        result = self.apply(events=[
            {"id": "fb-c", "deliveryNumber": "MOVE-C", "action": "complete", "actorName": "Boss"},
            {"id": "fb-d", "deliveryNumber": "MOVE-D", "action": "delete", "reason": "duplicate"},
        ])
        self.assertEqual([row["result"] for row in result["events"]], ["applied", "applied"])
        self.assertEqual(self.counters("MOVE-C")[1:], ("received", True))
        db = self.Session()
        self.assertIsNone(db.query(Delivery).filter(Delivery.delivery_number == "MOVE-D").one_or_none())
        audit = db.query(DeliveryDeletionAudit).filter(DeliveryDeletionAudit.delivery_number == "MOVE-D").one()
        self.assertEqual(audit.actor_uid, "firebase-bridge")
        db.close()


class ApplyDeliveryTests(BridgeTestCase):
    def payload(self, **overrides):
        data = {"deliveryNumber": "FB-1", "expectedPackages": 3,
                "sourceId": "firestore-only-id", "sourceName": "Основной склад", "sourceIsMain": True,
                "destinationName": "Магазин Balti", "comment": "fragile"}
        data.update(overrides)
        return data

    def test_create_maps_warehouses_by_name_and_marks_included_events(self):
        result = self.apply(deliveries=[self.payload(counters={"source_pick": 1}, status="partial_pick",
                                                     includedEventIds=["fb-old"])])
        self.assertEqual(result["deliveries"][0]["result"], "created")
        db = self.Session()
        delivery = db.query(Delivery).filter(Delivery.delivery_number == "FB-1").one()
        self.assertEqual(delivery.source_id, "00000000-0000-0000-0000-000000000001")
        self.assertEqual(delivery.destination_id, "00000000-0000-0000-0000-000000000002")
        db.close()
        self.assertEqual(self.counters("FB-1")[:2], ({"source_pick": 1}, "partial_pick"))
        # The pre-existing Firebase scan is already in the copied counter.
        replay = self.apply(events=[{"id": "fb-old", "deliveryNumber": "FB-1", "action": "pickup", "count": 1}])
        self.assertEqual(replay["events"][0]["result"], "duplicate")
        self.assertEqual(self.counters("FB-1")[0], {"source_pick": 1})

    def test_update_keeps_counters_and_refuses_shrinking_below_scans(self):
        self.add_delivery("FB-1", expected=3, picked=2, status=DeliveryStatus.partial_pick)
        result = self.apply(deliveries=[self.payload(expectedPackages=1, comment="new",
                                                     counters={"source_pick": 0})])["deliveries"][0]
        self.assertEqual(result["result"], "updated")
        self.assertIn("expectedPackages", result["detail"])
        counters, status, _ = self.counters("FB-1")
        self.assertEqual((counters, status), ({"source_pick": 2}, "partial_pick"))
        unchanged = self.apply(deliveries=[self.payload(expectedPackages=3, comment="new")])["deliveries"][0]
        self.assertEqual(unchanged["result"], "unchanged")

    def test_unknown_warehouse_is_a_conflict(self):
        result = self.apply(deliveries=[self.payload(destinationName="Nowhere")])
        self.assertEqual(result["deliveries"][0]["result"], "conflict")


class ChangesTests(BridgeTestCase):
    def test_changes_include_legacy_scans_and_exclude_bridge_events(self):
        delivery_id = self.add_delivery(expected=3, picked=1, status=DeliveryStatus.partial_pick)
        db = self.Session()
        db.add(ScanEvent(delivery_id=delivery_id, stage=ScanStage.source_pick,
                         scanned_by="00000000-0000-0000-0000-0000000000aa",
                         warehouse_id="00000000-0000-0000-0000-000000000001", count=1, created_at=utc(1)))
        db.add(DeliveryEvent(delivery_id=delivery_id, event_type="pickup", actor_uid="firebase-bridge",
                             actor_name="x", actor_role="driver", count=1, client_event_id="fb-1",
                             created_at=utc(1)))
        db.commit()
        db.close()
        body = self.client.get("/bridge/changes", headers=HEADERS,
                               params={"since": utc(2).isoformat()}).json()
        self.assertFalse(body["full"])
        self.assertEqual(len(body["events"]), 1)
        event = body["events"][0]
        self.assertEqual((event["action"], event["count"], event["actorName"]), ("pickup", 1, "ion"))
        self.assertEqual(body["deliveries"][0]["counters"], {"source_pick": 1})
        self.assertEqual(body["deliveries"][0]["eventIds"], [event["id"]])
        self.assertEqual(body["deliveries"][0]["source"]["isMain"], True)
        self.assertTrue(body["cursor"])

    def test_old_rows_are_not_repeated_and_deletions_are_reported(self):
        self.add_delivery("OLD", minutes_ago=60)
        db = self.Session()
        db.add(DeliveryDeletionAudit(delivery_number="GONE", actor_uid="supervisor-uid", actor_name="Boss",
                                     actor_role="supervisor", deleted_at=utc(1)))
        db.add(DeliveryDeletionAudit(delivery_number="BRIDGED", actor_uid="firebase-bridge", actor_name="x",
                                     actor_role="supervisor", deleted_at=utc(1)))
        db.commit()
        db.close()
        body = self.client.get("/bridge/changes", headers=HEADERS,
                               params={"since": utc(2).isoformat()}).json()
        self.assertEqual(body["deliveries"], [])
        self.assertEqual([row["deliveryNumber"] for row in body["deletions"]], ["GONE"])

    def test_full_snapshot_lists_only_active_rows(self):
        self.add_delivery("ACTIVE", picked=1, status=DeliveryStatus.partial_pick, minutes_ago=600)
        self.add_delivery("DONE", status=DeliveryStatus.received, minutes_ago=600)
        body = self.client.get("/bridge/changes", headers=HEADERS, params={"full": "true"}).json()
        self.assertTrue(body["full"])
        self.assertEqual([row["deliveryNumber"] for row in body["deliveries"]], ["ACTIVE"])
        self.assertEqual(body["events"], [])

    def test_lookup_reports_state(self):
        self.add_delivery("ACTIVE")
        self.add_delivery("DONE", status=DeliveryStatus.received)
        db = self.Session()
        db.add(DeliveryDeletionAudit(delivery_number="GONE", actor_uid="u", actor_name="n", actor_role="supervisor"))
        db.commit()
        db.close()
        rows = self.client.post("/bridge/lookup", headers=HEADERS,
                                json={"numbers": ["ACTIVE", "DONE", "GONE", "NEW"]}).json()["rows"]
        self.assertEqual([row["state"] for row in rows], ["active", "done", "deleted", "missing"])


class WarehouseKeyTests(unittest.TestCase):
    def test_matches_typescript_normalization(self):
        self.assertEqual(bridge.warehouse_match_key("Магазин  Централка"), "централка")
        self.assertEqual(bridge.warehouse_match_key("Gagarin 5/3"), "gagarin 5 3")
        self.assertEqual(bridge.warehouse_match_key("Shop Balti"), bridge.warehouse_match_key("balti"))


if __name__ == "__main__":
    unittest.main()


class ArchiveStaleTests(BridgeTestCase):
    def test_closes_only_deliveries_picked_long_ago(self):
        self.add_delivery("OLD", expected=2, picked=2, status=DeliveryStatus.picked, minutes_ago=10 * 24 * 60)
        self.add_delivery("RECENT", expected=2, picked=2, status=DeliveryStatus.picked, minutes_ago=2 * 24 * 60)
        self.add_delivery("WAITING", status=DeliveryStatus.created, minutes_ago=30 * 24 * 60)
        headers = {"X-Bridge-Secret": SECRET}
        dry = self.client.post("/bridge/archive_stale", json={"pickedOlderThanDays": 7, "dryRun": True},
                               headers=headers).json()
        self.assertEqual(dry["numbers"], ["OLD"])
        self.assertEqual(self.counters("OLD")[1], "picked", "a dry run changes nothing")
        done = self.client.post("/bridge/archive_stale", json={"pickedOlderThanDays": 7},
                                headers=headers).json()
        self.assertEqual(done["archived"], 1)
        _, status, archived = self.counters("OLD")
        self.assertEqual((status, archived), ("received", True))
        self.assertEqual(self.counters("RECENT")[1:], ("picked", False))
        self.assertEqual(self.counters("WAITING")[1:], ("created", False))
        changes = self.client.get("/bridge/changes", params={"since": utc(5).isoformat()}, headers=headers).json()
        old = [d for d in changes["deliveries"] if d["deliveryNumber"] == "OLD"]
        self.assertTrue(old and old[0]["done"], "Firebase learns about the close via /changes")

    def test_collector_name_is_sent_to_firebase(self):
        delivery_id = self.add_delivery("COLL")
        db = self.Session()
        db.query(Delivery).filter(Delivery.id == delivery_id).one().collector_name = "Carp Alexandru"
        db.commit()
        db.close()
        rows = self.client.post("/bridge/lookup", json={"numbers": ["COLL"]},
                                headers={"X-Bridge-Secret": SECRET}).json()["rows"]
        self.assertEqual(rows[0]["delivery"]["collectorName"], "Carp Alexandru")


class ArchiveNeverPickedTests(BridgeTestCase):
    def test_closes_never_picked_only_when_asked(self):
        self.add_delivery("NEVER", status=DeliveryStatus.created, minutes_ago=10 * 24 * 60)
        self.add_delivery("STARTED", expected=3, picked=1, status=DeliveryStatus.created, minutes_ago=10 * 24 * 60)
        self.add_delivery("FRESH", status=DeliveryStatus.created, minutes_ago=2 * 24 * 60)
        old_caller = self.client.post("/bridge/archive_stale", json={"pickedOlderThanDays": 7}, headers=HEADERS).json()
        self.assertEqual(old_caller["neverPicked"], 0, "older callers keep the picked-only rule")
        self.assertEqual(self.counters("NEVER")[1], "created")
        result = self.client.post("/bridge/archive_stale", json={"pickedOlderThanDays": 7, "createdOlderThanDays": 7},
                                  headers=HEADERS).json()
        self.assertEqual((result["neverPicked"], result["numbers"]), (1, ["NEVER"]))
        self.assertEqual(self.counters("NEVER")[1:], ("received", True))
        self.assertEqual(self.counters("STARTED")[1:], ("created", False), "a partly scanned delivery stays")
        self.assertEqual(self.counters("FRESH")[1:], ("created", False))
