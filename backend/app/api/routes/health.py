from fastapi import APIRouter

from app.db.mongo import get_db

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    await get_db().command("ping")
    return {"status": "ok"}

