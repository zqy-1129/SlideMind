from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, HTTPException

from app.db.mongo import get_db
from app.models.schemas import DatasetCreate, DatasetOut
from app.services.deletion import delete_dataset_cascade
from app.utils.ids import stringify_id

router = APIRouter()


@router.post("/datasets", response_model=DatasetOut)
async def create_dataset(payload: DatasetCreate) -> dict:
    now = datetime.utcnow()
    result = await get_db().datasets.insert_one(
        {"name": payload.name, "description": payload.description, "created_at": now}
    )
    document = await get_db().datasets.find_one({"_id": result.inserted_id})
    return stringify_id(document)


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets() -> list[dict]:
    cursor = get_db().datasets.find().sort("created_at", -1)
    return [stringify_id(document) async for document in cursor]


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: str) -> dict:
    document = await get_db().datasets.find_one({"_id": ObjectId(dataset_id)})
    if document is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return stringify_id(document)


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str) -> dict:
    result = await delete_dataset_cascade(dataset_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result
