from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Iterable, Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as firebase_auth, firestore

from .firebase import firebase_is_configured


ALLOWED_ROLES = {"driver", "manager", "supervisor"}
PROFILE_CACHE_SECONDS = 30
_profile_cache = {}
_profile_cache_lock = Lock()


@dataclass(frozen=True)
class FirebasePrincipal:
    uid: str
    name: str
    role: str
    warehouse_id: Optional[str]
    warehouse_name: str


def _bearer_token(authorization: Optional[str]) -> str:
    value = (authorization or "").strip()
    if not value.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase bearer token required",
        )
    token = value[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")
    return token


def get_firebase_principal(
    authorization: Optional[str] = Header(default=None),
) -> FirebasePrincipal:
    """Authenticate Firebase and read role/name/warehouse from users/{uid}.

    No email or legacy-user mapping is performed. The Firebase UID is the audit
    identity and the profile role controls access, as requested for the staged
    rollout.
    """
    if not firebase_is_configured():
        raise HTTPException(status_code=503, detail="Firebase Admin is not configured")

    token = _bearer_token(authorization)
    try:
        # Signature and expiry validation are local after Google's certificates
        # are cached. Firestore remains the authority for the current role.
        decoded = firebase_auth.verify_id_token(token, check_revoked=False)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token") from exc

    uid = str(decoded.get("uid") or decoded.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Firebase UID missing")

    now = monotonic()
    with _profile_cache_lock:
        cached = _profile_cache.get(uid)
        data = cached[1] if cached and cached[0] > now else None
    if data is None:
        try:
            profile = firestore.client().collection("users").document(uid).get()
            data = profile.to_dict() if profile.exists else None
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Cannot read Firebase user profile") from exc
        if data:
            with _profile_cache_lock:
                _profile_cache[uid] = (now + PROFILE_CACHE_SECONDS, data)

    if not data:
        raise HTTPException(status_code=403, detail="Firebase user profile missing")

    role = str(data.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Unsupported Firebase role")

    name = str(
        data.get("name")
        or data.get("displayName")
        or decoded.get("name")
        or uid
    ).strip()

    warehouse_id = str(
        data.get("warehouseId") or data.get("warehouse_id") or ""
    ).strip() or None
    if warehouse_id:
        try:
            UUID(warehouse_id)
        except ValueError:
            # Existing Firestore shop ids are not necessarily PostgreSQL UUIDs.
            # Role-only rollout treats those as unlinked instead of rejecting
            # every request or feeding an invalid UUID into PostgreSQL.
            warehouse_id = None
    warehouse_name = str(
        data.get("warehouseName") or data.get("warehouse_name") or ""
    ).strip()

    return FirebasePrincipal(
        uid=uid,
        name=name or uid,
        role=role,
        warehouse_id=warehouse_id,
        warehouse_name=warehouse_name,
    )


def require_roles(principal: FirebasePrincipal, roles: Iterable[str]) -> None:
    if principal.role not in set(roles):
        raise HTTPException(status_code=403, detail="Role is not allowed")
