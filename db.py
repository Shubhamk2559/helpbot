import re
import time
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from config import DB_NAME, MONGO_URI, SESSION_TTL, STATS_TZ_OFFSET_MIN

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]
bots = _db.bots
files = _db.files
sessions = _db.sessions
pending_deletes = _db.pending_deletes
stats = _db.stats


async def init():
    await bots.create_index("bot_id", unique=True)
    await bots.create_index("username")
    await files.create_index("token", unique=True)
    await sessions.create_index("sid", unique=True)
    await sessions.create_index("created", expireAfterSeconds=SESSION_TTL)
    await pending_deletes.create_index("delete_at")
    await stats.create_index("day", unique=True)


# ---------- bots registry ----------
async def upsert_bot(bot_id: str, username: str, token_enc: str, status: str):
    await bots.update_one(
        {"bot_id": bot_id},
        {
            "$set": {
                "username": username,
                "token_enc": token_enc,
                "status": status,
                "updated_at": time.time(),
            },
            "$setOnInsert": {"added_at": time.time()},
        },
        upsert=True,
    )


async def list_bots(status: str | None = None):
    query = {"status": status} if status else {}
    return await bots.find(query).sort("added_at", 1).to_list(length=200)


async def get_bot_by_username(username: str):
    username = username.lstrip("@")
    return await bots.find_one(
        {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}
    )


async def get_bot(bot_id: str):
    return await bots.find_one({"bot_id": bot_id})


async def set_status(bot_id: str, status: str):
    await bots.update_one(
        {"bot_id": bot_id}, {"$set": {"status": status, "updated_at": time.time()}}
    )


async def delete_bot(bot_id: str):
    await bots.delete_one({"bot_id": bot_id})


# ---------- files ----------
async def save_file(token: str, locations: list, caption: str):
    await files.insert_one(
        {
            "token": token,
            "locations": locations,
            "caption": caption,
            "created_at": time.time(),
        }
    )


async def get_file(token: str):
    return await files.find_one({"token": token})


async def count_files() -> int:
    return await files.count_documents({})


async def list_files(skip: int, limit: int):
    return await files.find().sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def delete_file(token: str):
    await files.delete_one({"token": token})


# ---------- verification sessions ----------
async def create_session(sid, token, user_id, chat_id, bot_id, required, prompt_msg=None):
    await sessions.insert_one(
        {
            "sid": sid,
            "token": token,
            "user_id": user_id,
            "chat_id": chat_id,
            "bot_id": bot_id,
            "ads_done": 0,
            "ads_required": required,
            "ad_started_at": None,
            "delivered": False,
            "prompt_msg": prompt_msg,
            "created": datetime.now(timezone.utc),
        }
    )


async def get_session(sid: str):
    return await sessions.find_one({"sid": sid})


async def start_ad(sid: str):
    await sessions.update_one(
        {"sid": sid, "delivered": False}, {"$set": {"ad_started_at": time.time()}}
    )


async def complete_ad(sid: str, min_seconds: int):
    cutoff = time.time() - min_seconds
    return await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "ad_started_at": {"$ne": None, "$lte": cutoff},
        },
        {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}},
        return_document=ReturnDocument.AFTER,
    )


async def claim_delivery(sid: str) -> bool:
    r = await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "$expr": {"$gte": ["$ads_done", "$ads_required"]},
        },
        {"$set": {"delivered": True}},
    )
    return r is not None


async def unclaim_delivery(sid: str):
    await sessions.update_one({"sid": sid}, {"$set": {"delivered": False}})


# ---------- auto-delete of delivered files ----------
async def schedule_delete(bot_id: int, chat_id: int, message_id: int, delete_at: float):
    await pending_deletes.insert_one(
        {"bot_id": bot_id, "chat_id": chat_id, "message_id": message_id, "delete_at": delete_at}
    )


async def due_deletes(now: float, limit: int = 100):
    return await pending_deletes.find({"delete_at": {"$lte": now}}).limit(limit).to_list(length=limit)


async def remove_delete(_id):
    await pending_deletes.delete_one({"_id": _id})


# ---------- posts (image + multiple videos behind one link) ----------
async def save_post(token: str, items: list, post_caption: str, photo: str):
    await files.insert_one(
        {
            "token": token,
            "kind": "post",
            "items": items,  # [{"locations": [...], "caption": str}, ...]
            "locations": [],
            "caption": f"📝 Post • {len(items)} video{'s' if len(items) != 1 else ''}",
            "post_caption": post_caption,
            "photo": photo,
            "views": 0,
            "created_at": time.time(),
        }
    )


# ---------- views (jitni baar file/videos user ko mile) ----------
def _day(offset_days: int = 0) -> str:
    now = datetime.now(timezone.utc) + timedelta(minutes=STATS_TZ_OFFSET_MIN)
    return (now - timedelta(days=offset_days)).strftime("%Y-%m-%d")


async def record_view(token: str):
    await stats.update_one({"day": _day()}, {"$inc": {"views": 1}}, upsert=True)
    await files.update_one({"token": token}, {"$inc": {"views": 1}})


async def views_today() -> int:
    d = await stats.find_one({"day": _day()})
    return int(d.get("views", 0)) if d else 0


async def views_total() -> int:
    r = await stats.aggregate([{"$group": {"_id": None, "t": {"$sum": "$views"}}}]).to_list(length=1)
    return int(r[0]["t"]) if r else 0


async def views_last_days(n: int = 7) -> list:
    wanted = [_day(i) for i in range(n)]
    docs = await stats.find({"day": {"$in": wanted}}).to_list(length=n)
    by_day = {d["day"]: int(d.get("views", 0)) for d in docs}
    return [(d, by_day.get(d, 0)) for d in wanted]
