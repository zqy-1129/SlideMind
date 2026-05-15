from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, HTTPException

from app.db.mongo import get_db
from app.models.schemas import GraphBuildOut, GraphBuildRequest, GraphOut, GraphTaskOut
from app.services.graph_builder import read_graph, read_graph_node_types
from app.services.graph_tasks import build_graph_task
from app.utils.ids import oid, stringify_id

router = APIRouter()


@router.post("/graph/build", response_model=GraphBuildOut)
async def build_graph(payload: GraphBuildRequest) -> dict:
    dataset = await get_db().datasets.find_one({"_id": ObjectId(payload.dataset_id)})
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    task_id = oid()
    now = datetime.utcnow()
    await get_db().graph_tasks.insert_one(
        {
            "_id": ObjectId(task_id),
            "dataset_id": payload.dataset_id,
            "include_text_kg": payload.include_text_kg,
            "status": "queued",
            "progress": 0,
            "logs": ["图谱生成任务已创建"],
            "summary": {},
            "created_at": now,
            "updated_at": now,
        }
    )
    build_graph_task.delay(task_id)
    return {"task_id": task_id, "status": "queued", "message": "Graph build queued"}


@router.get("/graph/tasks/{task_id}", response_model=GraphTaskOut)
async def get_graph_task(task_id: str) -> dict:
    task = await get_db().graph_tasks.find_one({"_id": ObjectId(task_id)})
    if task is None:
        raise HTTPException(status_code=404, detail="Graph task not found")
    return stringify_id(task)


@router.get("/graph", response_model=GraphOut)
async def get_graph(
    dataset_id: str | None = None,
    limit: int = 20,
    node_type: str | None = None,
    parent_id: str | None = None,
    show_all: bool = False,
) -> dict:
    return read_graph(dataset_id=dataset_id, limit=limit, node_type=node_type, parent_id=parent_id, show_all=show_all)


@router.get("/graph/node-types")
async def get_graph_node_types(dataset_id: str | None = None) -> dict:
    return {"types": read_graph_node_types(dataset_id=dataset_id)}
