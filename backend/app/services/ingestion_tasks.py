import asyncio

from app.worker.celery_app import celery_app


@celery_app.task(name="app.services.ingestion.ingest_file")
def ingest_file(task_id: str) -> None:
    from app.db.mongo import close_mongo, init_mongo
    from app.services.ingestion import run_ingestion

    async def runner() -> None:
        await init_mongo()
        await run_ingestion(task_id)
        await close_mongo()

    asyncio.run(runner())
