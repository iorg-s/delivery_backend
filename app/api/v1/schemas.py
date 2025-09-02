# app/api/v1/schemas.py
from pydantic import BaseModel
from enum import Enum

class ScanStage(str, Enum):
    source_pick = "source_pick"
    dest_arrival = "dest_arrival"
    dest_receive = "dest_receive"

class ScanRequest(BaseModel):
    delivery_number: str
    count: int
    stage: ScanStage

class TransferCreate(BaseModel):
    delivery_number: str
    destination_id: str   # warehouse id
    package_count: int