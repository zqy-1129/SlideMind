from datetime import datetime
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Header, HTTPException
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db
from app.models.schemas import TokenOut, UserCreate, UserLogin, UserOut
from app.utils.ids import stringify_id

router = APIRouter()


@router.post("/auth/register", response_model=UserOut)
async def register(payload: UserCreate) -> dict:
    if payload.role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="Role must be admin or user")
    salt = secrets.token_hex(16)
    now = datetime.utcnow()
    try:
        result = await get_db().users.insert_one(
            {
                "username": payload.username,
                "password_hash": _hash_password(payload.password, salt),
                "salt": salt,
                "role": payload.role,
                "created_at": now,
            }
        )
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    user = await get_db().users.find_one({"_id": result.inserted_id}, {"password_hash": 0, "salt": 0})
    return stringify_id(user)


@router.post("/auth/login", response_model=TokenOut)
async def login(payload: UserLogin) -> dict:
    user = await get_db().users.find_one({"username": payload.username})
    if user is None or not _verify_password(payload.password, user["salt"], user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = secrets.token_urlsafe(32)
    await get_db().sessions.insert_one(
        {
            "token": token,
            "user_id": str(user["_id"]),
            "created_at": datetime.utcnow(),
        }
    )
    public_user = stringify_id({key: value for key, value in user.items() if key not in {"password_hash", "salt"}})
    return {"access_token": token, "token_type": "bearer", "user": public_user}


@router.get("/auth/me", response_model=UserOut)
async def me(authorization: str | None = Header(default=None)) -> dict:
    token = _extract_token(authorization)
    session = await get_db().sessions.find_one({"token": token})
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await get_db().users.find_one({"_id": ObjectId(session["user_id"])}, {"password_hash": 0, "salt": 0})
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return stringify_id(user)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def _verify_password(password: str, salt: str, password_hash: str) -> bool:
    return hmac.compare_digest(_hash_password(password, salt), password_hash)


def _extract_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()
