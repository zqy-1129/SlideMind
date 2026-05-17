from typing import Any

from bson import ObjectId
from fastapi import APIRouter, HTTPException

from app.db.mongo import get_db
from app.services.environment_time_series import build_environment_time_series_document
from app.services.insar_time_series import build_insar_time_series_document
from app.services.deletion import delete_dataset_data
from app.utils.ids import stringify_id

router = APIRouter()


@router.get("/records")
async def list_tabular_records(
    dataset_id: str | None = None,
    data_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if dataset_id:
        query["dataset_id"] = dataset_id
    if data_type:
        query["data_type"] = data_type
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    total = await get_db().tabular_records.count_documents(query)
    cursor = (
        get_db()
        .tabular_records.find(query)
        .sort("row_number", 1)
        .skip((safe_page - 1) * safe_page_size)
        .limit(safe_page_size)
    )
    return {
        "items": [stringify_id(document) async for document in cursor],
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
    }


@router.get("/documents")
async def list_documents(dataset_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    query = {"dataset_id": dataset_id} if dataset_id else {}
    cursor = get_db().documents.find(query, {"content": 0}).sort("created_at", -1).limit(min(limit, 5000))
    return [stringify_id(document) async for document in cursor]


@router.get("/document-chunks")
async def list_document_chunks(dataset_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    query = {"dataset_id": dataset_id} if dataset_id else {}
    cursor = get_db().document_chunks.find(query).sort("chunk_index", 1).limit(min(limit, 5000))
    return [stringify_id(document) async for document in cursor]


@router.get("/gis-features")
async def list_gis_features(
    dataset_id: str | None = None,
    gis_category: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if dataset_id:
        query["dataset_id"] = dataset_id
    if gis_category:
        query["gis_category"] = gis_category
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    total = await get_db().gis_features.count_documents(query)
    cursor = (
        get_db()
        .gis_features.find(query)
        .sort("feature_index", 1)
        .skip((safe_page - 1) * safe_page_size)
        .limit(safe_page_size)
    )
    return {
        "items": [stringify_id(document) async for document in cursor],
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
    }


@router.get("/insar/time-series")
async def get_insar_time_series(record_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(record_id):
        raise HTTPException(status_code=400, detail="Invalid record_id")
    document = await get_db().insar_time_series.find_one({"source_record_id": record_id})
    if document is None:
        record = await get_db().tabular_records.find_one({"_id": ObjectId(record_id), "data_type": "insar"})
        document = build_insar_time_series_document(record) if record else None
    if document is None:
        raise HTTPException(status_code=404, detail="InSAR time series not found")
    document.setdefault("_id", ObjectId())
    return stringify_id(document)


@router.get("/environment/time-series")
async def get_environment_time_series(dataset_id: str, data_type: str) -> dict[str, Any]:
    if not ObjectId.is_valid(dataset_id):
        raise HTTPException(status_code=400, detail="Invalid dataset_id")
    if data_type not in {"rainfall", "water_level"}:
        raise HTTPException(status_code=400, detail="Unsupported environment data_type")

    document = await get_db().environment_time_series.find_one(
        {"dataset_id": dataset_id, "data_type": data_type},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if document is None:
        dataset = await get_db().datasets.find_one({"_id": ObjectId(dataset_id)})
        records = [
            record
            async for record in get_db()
            .tabular_records.find({"dataset_id": dataset_id, "data_type": data_type})
            .sort("timestamp", 1)
        ]
        document = build_environment_time_series_document(
            dataset_id,
            dataset.get("name") if dataset else dataset_id,
            records[0].get("source_file_id") if records else "legacy",
            data_type,
            records,
        )
    if document is None:
        raise HTTPException(status_code=404, detail="Environment time series not found")
    document.setdefault("_id", ObjectId())
    return stringify_id(document)


@router.delete("/data")
async def delete_data(dataset_id: str, data_kind: str) -> dict[str, Any]:
    if not dataset_id:
        raise HTTPException(status_code=400, detail="dataset_id is required")
    result = await delete_dataset_data(dataset_id, data_kind)
    if not result["deleted"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    return result
