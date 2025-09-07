# app/api/v1/schemas.py
from pydantic import BaseModel
from app.models import ScanStage  # ✅ reuse the real enum from models


class ScanRequest(BaseModel):
    delivery_number: str
    count: int
    stage: ScanStage   # ✅ now uses the same Enum as DB/models


class TransferCreate(BaseModel):
    delivery_number: str
    destination_id: str   # warehouse id
    package_count: int
