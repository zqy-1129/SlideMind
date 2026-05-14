import asyncio
from datetime import datetime

from bson import ObjectId

from app.worker.celery_app import celery_app


@celery_app.task(name="app.services.graph_tasks.build_graph")
def build_graph_task(task_id: str) -> None:
    from app.db.mongo import close_mongo, init_mongo
    from app.db.neo4j import close_neo4j, init_neo4j

    async def runner() -> None:
        await init_mongo()
        init_neo4j()
        try:
            await run_graph_task(task_id)
        finally:
            close_neo4j()
            await close_mongo()

    asyncio.run(runner())


async def run_graph_task(task_id: str) -> None:
    from app.db.mongo import get_db
    from app.services.graph_builder import build_graph_for_dataset

    database = get_db()
    task = await database.graph_tasks.find_one({"_id": ObjectId(task_id)})
    if task is None:
        raise ValueError(f"Graph task not found: {task_id}")

    dataset_id = task["dataset_id"]
    include_text_kg = bool(task.get("include_text_kg", True))

    async def log(message: str, progress: int) -> None:
        await database.graph_tasks.update_one(
            {"_id": ObjectId(task_id)},
            {
                "$set": {"progress": progress, "updated_at": datetime.utcnow()},
                "$push": {"logs": message},
            },
        )

    await database.graph_tasks.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": {"status": "running", "progress": 1, "updated_at": datetime.utcnow()},
            "$push": {"logs": "图谱生成任务开始"},
        },
    )

    try:
        summary = await build_graph_for_dataset(dataset_id, log=log, include_text_kg=include_text_kg)
        await database.graph_tasks.update_one(
            {"_id": ObjectId(task_id)},
            {
                "$set": {
                    "status": "completed",
                    "progress": 100,
                    "summary": summary,
                    "updated_at": datetime.utcnow(),
                },
                "$push": {"logs": "图谱生成任务完成"},
            },
        )
    except Exception as exc:
        await database.graph_tasks.update_one(
            {"_id": ObjectId(task_id)},
            {
                "$set": {
                    "status": "failed",
                    "progress": 100,
                    "error": str(exc),
                    "updated_at": datetime.utcnow(),
                },
                "$push": {"logs": f"图谱生成失败：{exc}"},
            },
        )
        raise
