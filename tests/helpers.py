import time

from ads import engine, service, settings, store
from ads.verify import Policy
from tests.fakemongo import FakeDB


class SessionsAPI:
    """Mirror of db.start_ad / db.complete_ad semantics over the fake collection."""
    def __init__(self, db, started_ago=30.0):
        self.db, self.started_ago = db, started_ago

    async def start_ad(self, sid, ymid=None, zone=None):
        await self.db["sessions"].update_one({"sid": sid, "delivered": False},
                                             {"$set": {"ad_started_at": time.time() - self.started_ago, "cur_ymid": ymid, "cur_zone": zone}})

    async def complete_ad(self, sid, min_seconds):
        cutoff = time.time() - min_seconds
        return await self.db["sessions"].find_one_and_update(
            {"sid": sid, "delivered": False, "ad_started_at": {"$ne": None, "$lte": cutoff}},
            {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}}, return_document="after")


class FakeMonetag:
    def __init__(self):
        self.ads, self.fails, self.completions = {}, [], []

    async def record_attempt(self, ymid, sid, zone, country=None):
        self.ads[ymid] = {"sid": sid, "zone": zone, "pb_seen": False, "pb_valued": False}

    async def get_ad(self, ymid):
        return self.ads.get(ymid)

    async def record_fail(self, ymid, sid, reason):
        self.fails.append((ymid, reason))

    async def record_completion(self, ymid):
        self.completions.append(ymid)


def setup_env(*, policy=None, cfg_patch=None, monetag_postback=False, adsgram_secret=False, started_ago=30.0, ads_required=1):
    db = FakeDB()
    store.set_database(db)
    engine.invalidate()
    settings.RUNTIME["monetag_postback"] = monetag_postback
    settings.RUNTIME["adsgram_secret"] = adsgram_secret
    pol = policy or Policy(monetag_postback=monetag_postback, adsgram_reward_secret=adsgram_secret, min_seconds=5, wait_seconds=20)
    mt = FakeMonetag()

    async def zone():
        return "111"

    service.configure(service.Deps(sessions=SessionsAPI(db, started_ago), policy=pol, monetag=mt, select_zone=zone,
                                   sdk_url=lambda z: f"https://sdk/{z}.js"))
    return db, mt


async def new_session(db, sid="S1", user_id=42, required=1):
    await db["sessions"].insert_one({"_id": sid, "sid": sid, "token": "tok", "user_id": user_id, "chat_id": user_id, "bot_id": "b",
                                     "ads_done": 0, "ads_required": required, "ad_started_at": None, "delivered": False})
    return await db["sessions"].find_one({"sid": sid})


async def fresh(db, sid="S1"):
    return await db["sessions"].find_one({"sid": sid})


async def configure(patch):
    """Apply a validated config patch (adsgram/tads need unit ids supplied through the patch)."""
    new, errors = await engine.update_config(patch)
    assert not errors, errors
    return new
