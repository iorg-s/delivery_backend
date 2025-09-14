import asyncio
import json
from datetime import datetime, date, timezone
from typing import Dict, List, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from .auth import JWT_SECRET, ALGORITHM
from .db import SessionLocal  # <- typical pattern. If your app.db uses a different name, adapt.
from .models import User, UserRole, Delivery, DriverRoute, Warehouse

router = APIRouter()
# In-memory store of connections:
# user_id -> list of dicts { "ws": WebSocket, "role": str, "warehouse_id": str | None }
connections: Dict[str, List[Dict[str, Any]]] = {}

# Background task handle
_watcher_task = None
_watcher_stop = False

# Poll interval (seconds)
POLL_INTERVAL = 5


def get_db_session() -> Session:
    """
    Create a new DB session for background tasks / internal use.
    Adjust if your app.db exports a different session factory.
    """
    return SessionLocal()


def decode_token_user(token: str):
    """
    Decode token and return dict with user_id, role, warehouse_id.
    Raises JWTError if invalid.
    """
    payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    user_id = payload.get("sub")
    if user_id is None:
        raise JWTError("sub missing")
    return {
        "user_id": user_id,
        "role": payload.get("role"),
        "warehouse_id": payload.get("warehouse_id"),
    }


async def register_connection(ws: WebSocket, user_id: str, role: str, warehouse_id: str | None):
    await ws.accept()
    entry = {"ws": ws, "role": role, "warehouse_id": warehouse_id}
    connections.setdefault(user_id, []).append(entry)


def unregister_connection(user_id: str, ws: WebSocket):
    conns = connections.get(user_id)
    if not conns:
        return
    # remove matching websocket
    new = [c for c in conns if c["ws"] is not ws]
    if new:
        connections[user_id] = new
    else:
        connections.pop(user_id, None)


async def send_json_safe(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        # if we fail to send, caller will remove connection later if needed
        pass


def _serialize_delivery(d: Delivery) -> dict:
    return {
        "id": d.id,
        "delivery_number": d.delivery_number,
        "source_id": d.source_id,
        "destination_id": d.destination_id,
        "expected_packages": d.expected_packages,
        "status": d.status.value if hasattr(d.status, "value") else d.status,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


async def notify_new_delivery(delivery: Delivery, db: Session):
    """
    Evaluate connected users and send notifications according to rules:
      - supervisors: all
      - manager: if delivery.destination == manager.warehouse_id
      - driver: if delivery.destination or source is in driver's route today
    """
    payload = {"type": "delivery_created", "delivery": _serialize_delivery(delivery)}

    # quick gather of connected users to avoid dict size changes during iteration
    snapshot = []
    for uid, conns in list(connections.items()):
        for c in conns:
            snapshot.append((uid, c))

    # We'll need route warehouses for drivers — cache results per driver to avoid repeated DB hits
    driver_routes_cache: Dict[str, List[str]] = {}

    for uid, c in snapshot:
        role = c["role"]
        ws = c["ws"]

        try:
            if role == UserRole.supervisor.value:
                await send_json_safe(ws, payload)
                continue

            if role == UserRole.manager.value:
                # manager.warehouse_id is stored in the connection metadata
                if c["warehouse_id"] and str(c["warehouse_id"]) == str(delivery.destination_id):
                    await send_json_safe(ws, payload)
                continue

            if role == UserRole.driver.value:
                # load today's route warehouses for this driver (cache per uid)
                if uid not in driver_routes_cache:
                    today = datetime.utcnow().date()
                    rows = db.query(DriverRoute.warehouse_id).filter(
                        DriverRoute.driver_id == uid,
                        DriverRoute.route_date == today
                    ).all()
                    driver_routes_cache[uid] = [r[0] for r in rows]

                route_warehouses = driver_routes_cache.get(uid, [])
                # notify if destination OR source in their route
                if (str(delivery.destination_id) in [str(w) for w in route_warehouses]) or \
                   (str(delivery.source_id) in [str(w) for w in route_warehouses]):
                    await send_json_safe(ws, payload)
                continue

            # other roles (if any) — skip
        except Exception:
            # if sending failed, try to cleanup connection
            try:
                unregister_connection(uid, ws)
            except Exception:
                pass


async def _watcher_loop():
    """
    Background loop that polls the deliveries table for new records since last_check.
    """
    last_check = datetime.now(timezone.utc)
    db = None
    try:
        db = get_db_session()
        while not _watcher_stop:
            try:
                # fetch deliveries created after last_check
                new_rows: List[Delivery] = db.query(Delivery).filter(
                    Delivery.created_at > last_check
                ).order_by(Delivery.created_at.asc()).all()

                if new_rows:
                    # update last_check to the newest created_at we processed
                    last_check = max(d.created_at for d in new_rows if d.created_at) or last_check

                    for d in new_rows:
                        # notify all relevant connected clients
                        await notify_new_delivery(d, db)

                await asyncio.sleep(POLL_INTERVAL)
            except Exception:
                # swallow and loop on — but short backoff to avoid tight loop
                await asyncio.sleep(POLL_INTERVAL)
    finally:
        if db:
            db.close()


@router.websocket("/ws/notifications")
async def websocket_notifications(ws: WebSocket):
    """
    WebSocket endpoint. Clients must connect with ?token=<JWT>.
    Example:
      ws://yourhost/ws/notifications?token=ey...
    """
    # extract token from query params
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)  # policy violation
        return

    # decode token and get user info
    try:
        info = decode_token_user(token)
        user_id = info["user_id"]
        role = info.get("role")
        warehouse_id = info.get("warehouse_id")
    except JWTError:
        await ws.close(code=1008)
        return

    # register socket
    await register_connection(ws, user_id, role, warehouse_id)

    try:
        # keep connection open; allow client pings/pongs.
        while True:
            # wait for some message to keep connection alive
            # clients may send "ping" or any heartbeat; we don't require content
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        unregister_connection(user_id, ws)
    except Exception:
        unregister_connection(user_id, ws)
        try:
            await ws.close()
        except Exception:
            pass

# This stays in memory only, disappears on server restart
user_fcm_tokens: dict[str, str] = {}

def register_fcm_token(user_id: str, token: str):
    user_fcm_tokens[user_id] = token

def get_all_driver_tokens(db):
    drivers = db.query(User).filter(User.role == "driver").all()
    return [user_fcm_tokens.get(d.id) for d in drivers if d.id in user_fcm_tokens]


# Startup/shutdown helpers to control watcher
async def start_watcher():
    global _watcher_task, _watcher_stop
    _watcher_stop = False
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(_watcher_loop())


async def stop_watcher():
    global _watcher_task, _watcher_stop
    _watcher_stop = True
    if _watcher_task:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except Exception:
            pass
