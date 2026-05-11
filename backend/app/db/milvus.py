import time

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

from app.core.config import settings


def init_milvus() -> None:
    last_error: Exception | None = None
    for _ in range(settings.db_init_retries):
        try:
            connections.connect(alias="default", host=settings.milvus_host, port=settings.milvus_port)
            if utility.has_collection(settings.milvus_collection):
                return

            fields = [
                FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
                FieldSchema(name="mongo_id", dtype=DataType.VARCHAR, max_length=128),
                FieldSchema(name="dataset_id", dtype=DataType.VARCHAR, max_length=128),
                FieldSchema(name="source_type", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=4096),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.embedding_dim),
            ]
            schema = CollectionSchema(fields=fields, description="SlideMind landslide knowledge vectors")
            collection = Collection(name=settings.milvus_collection, schema=schema)
            collection.create_index(
                field_name="embedding",
                index_params={"metric_type": "COSINE", "index_type": "AUTOINDEX", "params": {}},
            )
            return
        except Exception as exc:
            last_error = exc
            time.sleep(settings.db_init_retry_seconds)
    raise RuntimeError(f"Milvus initialization failed: {last_error}") from last_error


def get_collection() -> Collection:
    if not utility.has_collection(settings.milvus_collection):
        init_milvus()
    collection = Collection(settings.milvus_collection)
    collection.load()
    return collection
