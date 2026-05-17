from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, data, datasets, graph, health, imports, qa, spatial_map
from app.core.config import settings
from app.db.mongo import close_mongo, init_mongo
from app.db.neo4j import close_neo4j, init_neo4j
from app.db.milvus import init_milvus


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_mongo()
    init_neo4j()
    init_milvus()
    yield
    await close_mongo()
    close_neo4j()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix=settings.api_prefix, tags=["health"])
app.include_router(auth.router, prefix=settings.api_prefix, tags=["auth"])
app.include_router(datasets.router, prefix=settings.api_prefix, tags=["datasets"])
app.include_router(data.router, prefix=settings.api_prefix, tags=["data"])
app.include_router(imports.router, prefix=settings.api_prefix, tags=["imports"])
app.include_router(graph.router, prefix=settings.api_prefix, tags=["graph"])
app.include_router(qa.router, prefix=settings.api_prefix, tags=["qa"])
app.include_router(spatial_map.router, prefix=settings.api_prefix, tags=["map"])
