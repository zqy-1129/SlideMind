import asyncio

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


async def init_mongo() -> None:
    global client, db
    last_error: Exception | None = None
    for _ in range(settings.db_init_retries):
        try:
            client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
            db = client[settings.mongo_db]
            await ensure_indexes()
            return
        except Exception as exc:
            last_error = exc
            if client is not None:
                client.close()
            await asyncio.sleep(settings.db_init_retry_seconds)
    raise RuntimeError(f"MongoDB initialization failed: {last_error}") from last_error


async def ensure_indexes() -> None:
    database = get_db()
    await database.datasets.create_index("name")
    await database.uploaded_files.create_index("dataset_id")
    await database.import_tasks.create_index("file_id")
    await database.tabular_records.create_index([("dataset_id", 1), ("data_type", 1), ("timestamp", 1)])
    await database.documents.create_index("dataset_id")
    await database.document_chunks.create_index([("dataset_id", 1), ("source_file_id", 1)])
    await database.gis_features.create_index(
        [("dataset_id", 1), ("gis_category", 1), ("source_file_id", 1), ("feature_index", 1)]
    )
    await database.qa_records.create_index("created_at")
    await database.users.create_index("username", unique=True)
    await database.sessions.create_index("token", unique=True)


def get_db() -> AsyncIOMotorDatabase:
    if db is None:
        raise RuntimeError("MongoDB is not initialized")
    return db


async def close_mongo() -> None:
    if client is not None:
        client.close()
