from fastapi import APIRouter

from app.models.schemas import GraphBuildRequest, GraphOut
from app.services.graph_builder import build_graph_for_dataset, read_graph

router = APIRouter()


@router.post("/graph/build")
async def build_graph(payload: GraphBuildRequest) -> dict:
    summary = await build_graph_for_dataset(payload.dataset_id)
    return {"status": "ok", **summary}


@router.get("/graph", response_model=GraphOut)
async def get_graph(dataset_id: str | None = None, limit: int = 120) -> dict:
    return read_graph(dataset_id=dataset_id, limit=limit)

