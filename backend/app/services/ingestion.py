from datetime import datetime
from pathlib import Path

from bson import ObjectId

from app.db.milvus import get_collection
from app.db.mongo import get_db
from app.services.embedding import embed_text
from app.services.normalization import normalize_record
from app.services.parsers import (
    GIS_EXTENSIONS,
    TABLE_EXTENSIONS,
    geojson_features,
    geometry_bbox,
    geometry_centroid,
    parse_geojson,
    parse_table,
    parse_text,
    split_text,
)
from app.utils.ids import oid
from app.utils.mongo_values import clean_for_mongo

GIS_CATEGORY_NAMES = {
    "area": "行政区",
    "build": "建筑",
    "traffic": "交通",
    "water": "水域",
    "other": "其他",
}


async def run_ingestion(task_id: str) -> None:
    database = get_db()
    task = await database.import_tasks.find_one({"_id": ObjectId(task_id)})
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    file_doc = await database.uploaded_files.find_one({"_id": ObjectId(task["file_id"])})
    if file_doc is None:
        raise ValueError(f"File not found: {task['file_id']}")

    await _set_task_status(task_id, "running", "Import started")
    path = file_doc["path"]
    suffix = Path(path).suffix.lower()

    try:
        if task["data_type"] == "gis_vector" or suffix in GIS_EXTENSIONS:
            count = await _ingest_gis_vector(task, file_doc)
            await _set_task_status(task_id, "completed", f"Imported {count} GIS features")
        elif suffix in TABLE_EXTENSIONS:
            count = await _ingest_table(task, file_doc)
            await _set_task_status(task_id, "completed", f"Imported {count} table records")
        else:
            count = await _ingest_text(task, file_doc)
            await _set_task_status(task_id, "completed", f"Imported {count} text chunks")
    except Exception as exc:
        await _set_task_status(task_id, "failed", str(exc))
        raise


async def _ingest_table(task: dict, file_doc: dict) -> int:
    source_file_id = str(file_doc["_id"])
    await get_db().tabular_records.delete_many({"source_file_id": source_file_id})
    rows = parse_table(file_doc["path"])
    documents = []
    for index, row in enumerate(rows, start=1):
        row = clean_for_mongo(row)
        normalized = normalize_record(row)
        normalized = clean_for_mongo(normalized)
        documents.append(
            {
                "dataset_id": task["dataset_id"],
                "source_file_id": source_file_id,
                "row_number": index,
                "data_type": task["data_type"],
                "timestamp": normalized.get("timestamp"),
                "location": {
                    "longitude": normalized.get("longitude"),
                    "latitude": normalized.get("latitude"),
                },
                "raw_fields": row,
                "normalized_fields": normalized,
                "created_at": datetime.utcnow(),
            }
        )

    if documents:
        await get_db().tabular_records.insert_many(documents)
    return len(documents)


async def _ingest_text(task: dict, file_doc: dict) -> int:
    source_file_id = str(file_doc["_id"])
    old_documents = get_db().documents.find({"source_file_id": source_file_id}, {"_id": 1})
    old_document_ids = [str(document["_id"]) async for document in old_documents]
    await get_db().documents.delete_many({"source_file_id": source_file_id})
    await get_db().document_chunks.delete_many({"source_file_id": source_file_id})

    text = parse_text(file_doc["path"])
    document_id = oid()
    await get_db().documents.insert_one(
        {
            "_id": ObjectId(document_id),
            "dataset_id": task["dataset_id"],
            "source_file_id": source_file_id,
            "title": file_doc.get("filename"),
            "content": text,
            "created_at": datetime.utcnow(),
        }
    )

    chunks = split_text(text)
    chunk_docs = []
    vector_rows = []
    for index, chunk in enumerate(chunks):
        chunk_id = oid()
        vector_id = f"vec_{chunk_id}"
        chunk_docs.append(
            {
                "_id": ObjectId(chunk_id),
                "dataset_id": task["dataset_id"],
                "source_file_id": source_file_id,
                "document_id": document_id,
                "chunk_index": index,
                "text": chunk,
                "entity_ids": [],
                "milvus_vector_id": vector_id,
                "created_at": datetime.utcnow(),
            }
        )
        vector_rows.append(
            {
                "id": vector_id,
                "mongo_id": chunk_id,
                "dataset_id": task["dataset_id"],
                "source_type": "document_chunk",
                "text": chunk[:4096],
                "embedding": embed_text(chunk),
            }
        )

    if chunk_docs:
        await get_db().document_chunks.insert_many(chunk_docs)
        try:
            collection = get_collection()
            if old_document_ids:
                expr = f'source_type == "document_chunk" && mongo_id in {old_document_ids}'
                collection.delete(expr)
            collection.insert(vector_rows)
            collection.flush()
        except Exception as exc:
            await get_db().import_tasks.update_one(
                {"_id": task["_id"]},
                {"$push": {"logs": f"Milvus vector write skipped: {exc}"}},
            )
    return len(chunk_docs)


async def _ingest_gis_vector(task: dict, file_doc: dict) -> int:
    source_file_id = str(file_doc["_id"])
    await get_db().gis_features.delete_many({"source_file_id": source_file_id})
    geojson = parse_geojson(file_doc["path"])
    layer_name = geojson.get("name") or file_doc.get("filename") or "未命名图层"
    gis_category = task.get("gis_category") or file_doc.get("gis_category") or "other"
    gis_category_name = GIS_CATEGORY_NAMES.get(gis_category, "其他")
    features = geojson_features(geojson)

    documents = []
    for index, feature in enumerate(features, start=1):
        geometry = clean_for_mongo(feature.get("geometry"))
        properties = clean_for_mongo(feature.get("properties") or {})
        documents.append(
            {
                "dataset_id": task["dataset_id"],
                "source_file_id": source_file_id,
                "feature_index": index,
                "data_type": "gis_vector",
                "gis_category": gis_category,
                "gis_category_name": gis_category_name,
                "layer_name": layer_name,
                "geometry_type": geometry.get("type") if isinstance(geometry, dict) else None,
                "properties": properties,
                "geometry": geometry,
                "bbox": geometry_bbox(geometry),
                "centroid": geometry_centroid(geometry),
                "created_at": datetime.utcnow(),
            }
        )

    if documents:
        await get_db().gis_features.insert_many(documents)
    return len(documents)


async def _set_task_status(task_id: str, status: str, log: str) -> None:
    await get_db().import_tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": {"status": status, "updated_at": datetime.utcnow()},
            "$push": {"logs": log},
        },
    )
