from pathlib import Path
from typing import Any

from bson import ObjectId

from app.db.mongo import get_db


async def delete_dataset_cascade(dataset_id: str) -> dict[str, Any]:
    database = get_db()
    dataset = await database.datasets.find_one({"_id": ObjectId(dataset_id)})
    if dataset is None:
        return {"deleted": False, "reason": "Dataset not found"}

    files = [document async for document in database.uploaded_files.find({"dataset_id": dataset_id})]
    file_ids = [str(document["_id"]) for document in files]
    chunk_ids = [str(document["_id"]) async for document in database.document_chunks.find({"dataset_id": dataset_id}, {"_id": 1})]

    counts = {
        "datasets": (await database.datasets.delete_one({"_id": ObjectId(dataset_id)})).deleted_count,
        "uploaded_files": (await database.uploaded_files.delete_many({"dataset_id": dataset_id})).deleted_count,
        "import_tasks": (await database.import_tasks.delete_many({"dataset_id": dataset_id})).deleted_count,
        "tabular_records": (await database.tabular_records.delete_many({"dataset_id": dataset_id})).deleted_count,
        "insar_time_series": (await database.insar_time_series.delete_many({"dataset_id": dataset_id})).deleted_count,
        "environment_time_series": (await database.environment_time_series.delete_many({"dataset_id": dataset_id})).deleted_count,
        "documents": (await database.documents.delete_many({"dataset_id": dataset_id})).deleted_count,
        "document_chunks": (await database.document_chunks.delete_many({"dataset_id": dataset_id})).deleted_count,
        "text_kg_tuples": (await database.text_kg_tuples.delete_many({"dataset_id": dataset_id})).deleted_count,
        "gis_features": (await database.gis_features.delete_many({"dataset_id": dataset_id})).deleted_count,
        "graph_tasks": (await database.graph_tasks.delete_many({"dataset_id": dataset_id})).deleted_count,
        "qa_records": (await database.qa_records.delete_many({"dataset_id": dataset_id})).deleted_count,
    }

    removed_files = _remove_files(files)
    graph_deleted = _delete_graph_dataset(dataset_id)
    vector_deleted = _delete_vectors_by_chunk_ids(chunk_ids)

    return {
        "deleted": True,
        "dataset_id": dataset_id,
        "file_ids": file_ids,
        "counts": counts,
        "removed_files": removed_files,
        "graph_deleted": graph_deleted,
        "vector_deleted": vector_deleted,
    }


async def delete_import_cascade(task_id: str) -> dict[str, Any]:
    database = get_db()
    task = await database.import_tasks.find_one({"_id": ObjectId(task_id)})
    if task is None:
        return {"deleted": False, "reason": "Import task not found"}

    file_id = task["file_id"]
    file_doc = await database.uploaded_files.find_one({"_id": ObjectId(file_id)})
    chunk_ids = [
        str(document["_id"])
        async for document in database.document_chunks.find({"source_file_id": file_id}, {"_id": 1})
    ]

    counts = {
        "import_tasks": (await database.import_tasks.delete_one({"_id": ObjectId(task_id)})).deleted_count,
        "uploaded_files": (await database.uploaded_files.delete_one({"_id": ObjectId(file_id)})).deleted_count,
        "tabular_records": (await database.tabular_records.delete_many({"source_file_id": file_id})).deleted_count,
        "insar_time_series": (await database.insar_time_series.delete_many({"source_file_id": file_id})).deleted_count,
        "environment_time_series": (
            await database.environment_time_series.delete_many(
                {
                    "$or": [
                        {"source_file_id": file_id},
                        {"dataset_id": task.get("dataset_id"), "data_type": task.get("data_type")},
                    ]
                }
            )
        ).deleted_count,
        "documents": (await database.documents.delete_many({"source_file_id": file_id})).deleted_count,
        "document_chunks": (await database.document_chunks.delete_many({"source_file_id": file_id})).deleted_count,
        "text_kg_tuples": (await database.text_kg_tuples.delete_many({"source_file_id": file_id})).deleted_count,
        "gis_features": (await database.gis_features.delete_many({"source_file_id": file_id})).deleted_count,
    }

    removed_files = _remove_files([file_doc] if file_doc else [])
    vector_deleted = _delete_vectors_by_chunk_ids(chunk_ids)

    return {
        "deleted": True,
        "task_id": task_id,
        "file_id": file_id,
        "counts": counts,
        "removed_files": removed_files,
        "vector_deleted": vector_deleted,
    }


async def delete_dataset_data(dataset_id: str, data_kind: str) -> dict[str, Any]:
    database = get_db()
    if data_kind in {"insar", "water_level", "rainfall"}:
        files = [document async for document in database.uploaded_files.find({"dataset_id": dataset_id, "data_type": data_kind})]
        file_ids = [str(document["_id"]) for document in files]
        result = await database.tabular_records.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
        series = await database.insar_time_series.delete_many({"dataset_id": dataset_id}) if data_kind == "insar" else None
        environment_series = (
            await database.environment_time_series.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
            if data_kind in {"water_level", "rainfall"}
            else None
        )
        tasks = await database.import_tasks.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
        uploaded = await database.uploaded_files.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
        removed_files = _remove_files(files)
        graph_deleted = _delete_graph_dataset(dataset_id)
        return {
            "deleted": True,
            "data_kind": data_kind,
            "file_ids": file_ids,
            "counts": {
                "tabular_records": result.deleted_count,
                "insar_time_series": series.deleted_count if series else 0,
                "environment_time_series": environment_series.deleted_count if environment_series else 0,
                "import_tasks": tasks.deleted_count,
                "uploaded_files": uploaded.deleted_count,
            },
            "removed_files": removed_files,
            "graph_deleted": graph_deleted,
        }

    if data_kind == "gis_vector":
        files = [document async for document in database.uploaded_files.find({"dataset_id": dataset_id, "data_type": data_kind})]
        file_ids = [str(document["_id"]) for document in files]
        features = await database.gis_features.delete_many({"dataset_id": dataset_id})
        tasks = await database.import_tasks.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
        uploaded = await database.uploaded_files.delete_many({"dataset_id": dataset_id, "data_type": data_kind})
        removed_files = _remove_files(files)
        graph_deleted = _delete_graph_dataset(dataset_id)
        return {
            "deleted": True,
            "data_kind": data_kind,
            "file_ids": file_ids,
            "counts": {
                "gis_features": features.deleted_count,
                "import_tasks": tasks.deleted_count,
                "uploaded_files": uploaded.deleted_count,
            },
            "removed_files": removed_files,
            "graph_deleted": graph_deleted,
        }

    if data_kind in {"documents", "chunks", "tuples"}:
        files = [document async for document in database.uploaded_files.find({"dataset_id": dataset_id, "data_type": "document"})]
        file_ids = [str(document["_id"]) for document in files]
        chunk_ids = [
            str(document["_id"])
            async for document in database.document_chunks.find({"dataset_id": dataset_id}, {"_id": 1})
        ]
        documents = await database.documents.delete_many({"dataset_id": dataset_id})
        chunks = await database.document_chunks.delete_many({"dataset_id": dataset_id})
        text_tuples = await database.text_kg_tuples.delete_many({"dataset_id": dataset_id})
        tasks = await database.import_tasks.delete_many({"dataset_id": dataset_id, "data_type": "document"})
        uploaded = await database.uploaded_files.delete_many({"dataset_id": dataset_id, "data_type": "document"})
        removed_files = _remove_files(files)
        vector_deleted = _delete_vectors_by_chunk_ids(chunk_ids)
        graph_deleted = _delete_graph_dataset(dataset_id)
        return {
            "deleted": True,
            "data_kind": data_kind,
            "file_ids": file_ids,
            "counts": {
                "documents": documents.deleted_count,
                "document_chunks": chunks.deleted_count,
                "text_kg_tuples": text_tuples.deleted_count,
                "import_tasks": tasks.deleted_count,
                "uploaded_files": uploaded.deleted_count,
            },
            "removed_files": removed_files,
            "vector_deleted": vector_deleted,
            "graph_deleted": graph_deleted,
        }

    return {"deleted": False, "reason": "Unsupported data kind"}


def _remove_files(files: list[dict[str, Any]]) -> list[str]:
    removed: list[str] = []
    for file_doc in files:
        path = file_doc.get("path") if file_doc else None
        if not path:
            continue
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            removed.append(str(file_path))
    return removed


def _delete_graph_dataset(dataset_id: str) -> bool:
    try:
        from app.db.neo4j import get_driver

        with get_driver().session() as session:
            session.run("MATCH (n {dataset_id: $dataset_id}) DETACH DELETE n", dataset_id=dataset_id)
        return True
    except Exception:
        return False


def _delete_vectors_by_chunk_ids(chunk_ids: list[str]) -> bool:
    if not chunk_ids:
        return True
    try:
        from app.db.milvus import get_collection

        quoted = ", ".join(f'"{chunk_id}"' for chunk_id in chunk_ids)
        get_collection().delete(f"mongo_id in [{quoted}]")
        return True
    except Exception:
        return False
