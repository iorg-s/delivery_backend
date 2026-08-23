import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Delivery


BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
DOCUMENT_TYPES = ("move", "demand", "purchaseorder")


def _headers() -> Dict[str, str]:
    token = str(os.getenv("MOYSKLAD_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="MoySklad token is not configured")
    authorization = token if " " in token else f"Bearer {token}"
    return {
        "Authorization": authorization,
        "Accept": "application/json;charset=utf-8",
    }


def _get(path_or_url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}{path_or_url}"
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except HTTPException:
        raise
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="MoySklad request timed out") from exc
    except requests.RequestException as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        detail = "MoySklad request failed"
        if exc.response is not None:
            try:
                errors = exc.response.json().get("errors") or []
                if errors:
                    detail = str(errors[0].get("error") or detail)
            except Exception:
                pass
        raise HTTPException(status_code=429 if status_code == 429 else 502, detail=detail) from exc


def _places(document: Dict[str, Any]) -> int | None:
    for attribute in document.get("attributes") or []:
        if str(attribute.get("name") or "").strip().lower() == "количество мест":
            value = attribute.get("value")
            if isinstance(value, (int, float)) and int(value) > 0:
                return int(value)
    return None


def _entity_name(entity: Any) -> str:
    if not isinstance(entity, dict):
        return ""
    inline = str(entity.get("name") or "").strip()
    if inline:
        return inline
    href = str((entity.get("meta") or {}).get("href") or "").strip()
    if not href:
        return ""
    try:
        return str(_get(href).get("name") or "").strip()
    except HTTPException:
        return ""


def _positions(document_type: str, document_id: str) -> List[Dict[str, Any]]:
    data = _get(
        f"/entity/{document_type}/{document_id}/positions",
        {"limit": 1000, "expand": "assortment"},
    )
    result: List[Dict[str, Any]] = []
    for row in data.get("rows") or []:
        assortment = row.get("assortment") or {}
        name = str(assortment.get("name") or assortment.get("code") or "").strip()
        if not name:
            continue
        quantity = row.get("quantity") or 0
        result.append({"name": name, "quantity": quantity})
    return result


def _short_items(rows: List[Dict[str, Any]], limit: int = 5) -> Dict[str, Any]:
    grouped: Dict[str, float] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        try:
            quantity = float(row.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        grouped[name] = grouped.get(name, 0) + quantity
    items = [
        {"name": name, "quantity": int(qty) if qty.is_integer() else qty}
        for name, qty in list(grouped.items())[:limit]
    ]
    return {"items": items, "remaining": max(0, len(grouped) - len(items))}


def find_delivery_documents(number: str) -> List[Dict[str, Any]]:
    clean_number = str(number or "").strip()
    if not clean_number:
        raise HTTPException(status_code=422, detail="Delivery number is required")

    matches: List[Dict[str, Any]] = []
    for document_type in DOCUMENT_TYPES:
        listing = _get(
            f"/entity/{document_type}",
            {"filter": f"name={clean_number}", "limit": 10},
        )
        for row in listing.get("rows") or []:
            if str(row.get("name") or "").strip() != clean_number:
                continue
            document_id = str(row.get("id") or "").strip()
            if not document_id:
                continue
            detail = _get(
                f"/entity/{document_type}/{document_id}",
                {"expand": "sourceStore,targetStore,store,agent"},
            )
            source = detail.get("sourceStore") or detail.get("store") or {}
            destination = detail.get("targetStore") or {}
            company_name = _entity_name(detail.get("agent"))
            item_data = _short_items(_positions(document_type, document_id))
            matches.append({
                "documentType": document_type,
                "documentId": document_id,
                "documentNumber": str(detail.get("name") or clean_number),
                "companyName": company_name,
                "sourceId": str(source.get("id") or ""),
                "sourceName": _entity_name(source),
                "destinationId": str(destination.get("id") or ""),
                "destinationName": _entity_name(destination),
                "expectedPackages": _places(detail),
                "items": item_data["items"],
                "remainingItems": item_data["remaining"],
            })
    return matches


def enrich_delivery(db: Session, delivery_id: str) -> None:
    delivery = db.query(Delivery).filter(Delivery.id == delivery_id).one_or_none()
    if delivery is None or delivery.archived_at is not None:
        return
    delivery.enrichment_attempts = int(delivery.enrichment_attempts or 0) + 1
    delivery.enrichment_status = "running"
    delivery.enrichment_error = None
    db.commit()

    try:
        matches = find_delivery_documents(delivery.delivery_number)
        delivery = db.query(Delivery).filter(Delivery.id == delivery_id).one()
        if len(matches) == 0:
            delivery.enrichment_status = "not_found"
        elif len(matches) > 1:
            delivery.enrichment_status = "ambiguous"
            delivery.item_summary = {"matches": matches}
        else:
            match = matches[0]
            delivery.moysklad_document_type = match["documentType"]
            delivery.moysklad_document_id = match["documentId"]
            delivery.moysklad_document_name = match["documentNumber"]
            delivery.company_name = match["companyName"]
            delivery.item_summary = {
                "items": match["items"],
                "remaining": match["remainingItems"],
            }
            delivery.enrichment_status = "completed"
            delivery.enriched_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        delivery = db.query(Delivery).filter(Delivery.id == delivery_id).one_or_none()
        if delivery is not None:
            delivery.enrichment_status = "failed"
            delivery.enrichment_error = str(getattr(exc, "detail", exc))[:500]
            db.commit()
