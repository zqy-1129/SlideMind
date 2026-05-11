from datetime import datetime
from pathlib import Path

from bson import ObjectId
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import settings
from app.db.mongo import get_db
from app.models.schemas import ImportOut, ImportTaskOut
from app.services.deletion import delete_import_cascade
from app.services.ingestion import run_ingestion
from app.services.ingestion_tasks import ingest_file
from app.utils.ids import oid, stringify_id

router = APIRouter()


@router.post("/imports", response_model=ImportOut)
async def create_import(
    dataset_id: str = Form(...),
    data_type: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    dataset = await get_db().datasets.find_one({"_id": ObjectId(dataset_id)})
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    file_id = oid()
    task_id = oid()
    suffix = Path(file.filename or "").suffix.lower()
    stored_path = settings.upload_dir / f"{file_id}{suffix}"
    stored_path.write_bytes(await file.read())
    now = datetime.utcnow()

    await get_db().uploaded_files.insert_one(
        {
            "_id": ObjectId(file_id),
            "dataset_id": dataset_id,
            "filename": file.filename,
            "content_type": file.content_type,
            "path": str(stored_path),
            "data_type": data_type,
            "created_at": now,
        }
    )
    await get_db().import_tasks.insert_one(
        {
            "_id": ObjectId(task_id),
            "file_id": file_id,
            "dataset_id": dataset_id,
            "data_type": data_type,
            "status": "queued",
            "error_rows": [],
            "logs": ["Import task created"],
            "created_at": now,
            "updated_at": now,
        }
    )

    if settings.async_imports:
        ingest_file.delay(task_id)
        return {"task_id": task_id, "file_id": file_id, "status": "queued", "message": "Import queued"}

    try:
        await run_ingestion(task_id)
    except Exception as exc:
        task = await get_db().import_tasks.find_one({"_id": ObjectId(task_id)})
        return {
            "task_id": task_id,
            "file_id": file_id,
            "status": task["status"] if task else "failed",
            "message": str(exc),
        }
    task = await get_db().import_tasks.find_one({"_id": ObjectId(task_id)})
    return {
        "task_id": task_id,
        "file_id": file_id,
        "status": task["status"],
        "message": task["logs"][-1] if task.get("logs") else "Import completed",
    }


@router.get("/imports", response_model=list[ImportTaskOut])
async def list_imports(dataset_id: str | None = None) -> list[dict]:
    query = {"dataset_id": dataset_id} if dataset_id else {}
    cursor = get_db().import_tasks.find(query).sort("created_at", -1)
    return [stringify_id(document) async for document in cursor]


@router.post("/imports/{task_id}/retry", response_model=ImportOut)
async def retry_import(task_id: str) -> dict:
    task = await get_db().import_tasks.find_one({"_id": ObjectId(task_id)})
    if task is None:
        raise HTTPException(status_code=404, detail="Import task not found")

    await get_db().import_tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": {"status": "queued", "updated_at": datetime.utcnow()},
            "$push": {"logs": "Import task re-queued"},
        },
    )
    if settings.async_imports:
        ingest_file.delay(task_id)
        status = "queued"
        message = "Import re-queued"
    else:
        try:
            await run_ingestion(task_id)
        except Exception as exc:
            task = await get_db().import_tasks.find_one({"_id": ObjectId(task_id)})
            return {
                "task_id": task_id,
                "file_id": task["file_id"] if task else "",
                "status": task["status"] if task else "failed",
                "message": str(exc),
            }
        task = await get_db().import_tasks.find_one({"_id": ObjectId(task_id)})
        status = task["status"]
        message = task["logs"][-1] if task.get("logs") else "Import completed"
    return {
        "task_id": task_id,
        "file_id": task["file_id"],
        "status": status,
        "message": message,
    }


@router.delete("/imports/{task_id}")
async def delete_import(task_id: str) -> dict:
    result = await delete_import_cascade(task_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result
