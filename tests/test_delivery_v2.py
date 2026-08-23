import unittest

from fastapi import HTTPException

from app.api.v2.deliveries import DeliveryCreate, ScanRequestV2
from app.auth_v2 import FirebasePrincipal, _bearer_token, require_roles
from app.services.moysklad_delivery import _places, _short_items


class FirebaseRoleOnlyAuthTests(unittest.TestCase):
    def test_bearer_token_is_extracted(self):
        self.assertEqual(_bearer_token("Bearer firebase-token"), "firebase-token")

    def test_missing_bearer_token_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            _bearer_token("legacy-token")
        self.assertEqual(raised.exception.status_code, 401)

    def test_role_gate_does_not_require_legacy_user_mapping(self):
        principal = FirebasePrincipal(
            uid="firebase-uid",
            name="Driver One",
            role="driver",
            warehouse_id=None,
            warehouse_name="",
        )
        require_roles(principal, ["driver"])
        with self.assertRaises(HTTPException) as raised:
            require_roles(principal, ["supervisor"])
        self.assertEqual(raised.exception.status_code, 403)


class MoySkladDeliveryTests(unittest.TestCase):
    def test_places_attribute_is_detected(self):
        document = {
            "attributes": [
                {"name": "Комментарий", "value": "x"},
                {"name": "Количество мест", "value": 7},
            ]
        }
        self.assertEqual(_places(document), 7)

    def test_item_summary_groups_and_limits_rows(self):
        summary = _short_items(
            [
                {"name": "A", "quantity": 1},
                {"name": "A", "quantity": 2},
                {"name": "B", "quantity": 1.5},
                {"name": "C", "quantity": 1},
            ],
            limit=2,
        )
        self.assertEqual(summary["items"][0], {"name": "A", "quantity": 3})
        self.assertEqual(summary["items"][1], {"name": "B", "quantity": 1.5})
        self.assertEqual(summary["remaining"], 1)


class RequestContractTests(unittest.TestCase):
    def test_create_defaults_are_backward_compatible(self):
        payload = DeliveryCreate(
            deliveryNumber="MOVE-1",
            destinationId="destination-id",
        )
        self.assertEqual(payload.expectedPackages, 1)
        self.assertEqual(payload.type, "standard")

    def test_scan_action_is_strict(self):
        with self.assertRaises(ValueError):
            ScanRequestV2(deliveryNumber="MOVE-1", action="unknown")


if __name__ == "__main__":
    unittest.main()
