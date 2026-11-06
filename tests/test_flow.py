import random
import time
import unittest

from ads import engine, service, settings, store
from ads.verify import Policy
from tests.helpers import configure, fresh, new_session, setup_env

BOTH = {"networks": {"adsgram": {"enabled": True, "unit_id": "777", "allow_client_reward": True}, "tads": {"enabled": True, "unit_id": "w1", "allow_client_reward": True}}}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(lambda: settings.RUNTIME.update(monetag_postback=False, adsgram_secret=False))


class StartAndFallback(Base):
    async def test_single_monetag_start_payload(self):
        db, mt = setup_env()
        s = await new_session(db)
        r = await service.start(s, 42)
        self.assertTrue(r["ok"])
        self.assertEqual((r["network"], r["zone"], r["ymid"]), ("monetag", "111", r["ad_id"]))
        self.assertIn(r["ad_id"], mt.ads)                                   # legacy Monetag analytics still fed
        self.assertEqual((await fresh(db))["cur_ymid"], r["ad_id"])         # legacy session fields still set

    async def test_only_one_live_attempt_per_session(self):
        db, _ = setup_env()
        s = await new_session(db)
        self.assertTrue((await service.start(s, 42))["ok"])
        second = await service.start(s, 42)
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "busy")

    async def test_stale_unshown_attempt_is_superseded(self):
        db, _ = setup_env()
        s = await new_session(db)
        a = await service.start(s, 42)
        await db["sessions"].update_one({"sid": "S1"}, {"$set": {"smart_active_until": time.time() - 1}})   # lock expired
        b = await service.start(s, 42)
        self.assertTrue(b["ok"])
        self.assertEqual((await store.get_attempt(a["ad_id"]))["reason"], "superseded")

    async def test_fallback_chain_when_ad_never_displayed(self):
        db, _ = setup_env()
        await configure(dict(BOTH, mode="priority", priority=["adsgram", "tads", "monetag"]))
        s = await new_session(db)
        a = await service.start(s, 42)
        self.assertEqual(a["network"], "adsgram")
        r1 = await service.report(s, a["ad_id"], "fail", "no_fill")
        self.assertEqual(r1["next"]["network"], "tads")
        r2 = await service.report(s, r1["next"]["ad_id"], "fail", "error")
        self.assertEqual(r2["next"]["network"], "monetag")
        r3 = await service.report(s, r2["next"]["ad_id"], "fail", "timeout")
        self.assertTrue(r3["exhausted"])                                     # max_attempts = 3
        self.assertNotIn("next", r3)
        d = await fresh(db)
        self.assertIsNone(d["smart_active"])                                 # slot released, user can retry manually

    async def test_never_second_ad_after_one_was_displayed(self):
        db, _ = setup_env()
        await configure(dict(BOTH, mode="priority", priority=["adsgram", "monetag", "tads"]))
        s = await new_session(db)
        a = await service.start(s, 42)
        await service.report(s, a["ad_id"], "shown")
        r = await service.report(s, a["ad_id"], "fail", "error")
        self.assertTrue(r["exhausted"] and r["displayed"])
        self.assertNotIn("next", r)

    async def test_fallback_disabled(self):
        db, _ = setup_env()
        await configure(dict(BOTH, mode="priority", priority=["adsgram", "tads", "monetag"], fallback_enabled=False))
        s = await new_session(db)
        a = await service.start(s, 42)
        self.assertTrue((await service.report(s, a["ad_id"], "fail", "no_fill"))["exhausted"])

    async def test_fail_report_is_idempotent_and_ownership_checked(self):
        db, _ = setup_env()
        await configure(dict(BOTH, mode="priority", priority=["adsgram", "tads", "monetag"]))
        s = await new_session(db)
        a = await service.start(s, 42)
        n1 = (await service.report(s, a["ad_id"], "fail", "no_fill"))["next"]
        again = await service.report(s, a["ad_id"], "fail", "no_fill")
        self.assertNotIn("next", again)                                      # replay cannot spawn another attempt
        other = dict(s, user_id=99)
        self.assertFalse((await service.report(other, n1["ad_id"], "shown"))["ok"])
        other_sid = dict(s, sid="OTHER")
        self.assertFalse((await service.report(other_sid, n1["ad_id"], "shown"))["ok"])

    async def test_manual_mode_forces_first_pick(self):
        db, _ = setup_env()
        await configure(dict(BOTH, mode="manual", manual_network="tads"))
        for i in range(20):
            s = await new_session(db, sid=f"M{i}")
            self.assertEqual((await service.start(s, 42))["network"], "tads")

    async def test_no_network_available(self):
        db, _ = setup_env()
        await configure({"networks": {"monetag": {"enabled": False}}})
        s = await new_session(db)
        self.assertEqual((await service.start(s, 42))["code"], "no_network")

    async def test_tads_not_eligible_until_client_rewards_allowed(self):
        db, _ = setup_env()
        await configure({"networks": {"tads": {"enabled": True, "unit_id": "w1"}}})
        cfg = await engine.get_config(force=True)
        self.assertNotIn("tads", engine.eligible(cfg))                       # client-attested + not allowed => never selected
        await configure({"networks": {"tads": {"allow_client_reward": True}}})
        self.assertIn("tads", engine.eligible(await engine.get_config(force=True)))


class Rewards(Base):
    async def start(self, db, patch=None, **env):
        _db, mt = setup_env(**env) if not db else (db, None)
        return None

    async def grant_setup(self, **env):
        db, mt = setup_env(**env)
        s = await new_session(db)
        return db, mt, s

    async def test_monetag_legacy_grant_once_and_duplicate_blocked(self):
        db, mt, s = await self.grant_setup()
        a = await service.start(s, 42)
        await service.report(s, a["ad_id"], "shown")
        s = await fresh(db)
        r = await service.done(s, a["ad_id"], True)
        self.assertTrue(r["granted"])
        self.assertEqual(r["trust"], "client_attested")
        self.assertEqual(r["s2"]["ads_done"], 1)
        dup = await service.done(await fresh(db), a["ad_id"], True)          # replay
        self.assertFalse(dup["ok"])
        self.assertEqual((await fresh(db))["ads_done"], 1)
        self.assertEqual(len(db["ad_rewards"].docs), 1)
        self.assertEqual(mt.completions, [a["ad_id"]])

    async def test_monetag_postback_mode_requires_valued_postback(self):
        db, mt, s = await self.grant_setup(monetag_postback=True)
        a = await service.start(s, 42)
        s = await fresh(db)
        self.assertEqual((await service.done(s, a["ad_id"], True))["wait"], 2)      # no postback yet => wait, not grant
        self.assertEqual(len(db["ad_rewards"].docs), 0)
        mt.ads[a["ad_id"]].update(pb_seen=True, pb_valued=True)
        r = await service.done(s, a["ad_id"], True)
        self.assertEqual((r["granted"], r["trust"]), (True, "server_postback"))

    async def test_monetag_postback_never_arrives_is_denied(self):
        pol = Policy(monetag_postback=True, min_seconds=5, wait_seconds=0)
        db, mt, s = await self.grant_setup(monetag_postback=True, policy=pol)
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertFalse(r["ok"])
        self.assertEqual((await store.get_attempt(a["ad_id"]))["reason"], "unverified")
        self.assertEqual((await fresh(db))["ads_done"], 0)

    async def test_monetag_non_valued_is_denied(self):
        db, mt, s = await self.grant_setup()
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], True, non_valued=True)
        self.assertFalse(r["ok"])
        self.assertEqual((await fresh(db))["ads_done"], 0)
        self.assertIn((a["ad_id"], "non_valued"), mt.fails)

    async def test_not_completed_is_abandoned_never_rewarded(self):
        db, mt, s = await self.grant_setup()
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], False)
        self.assertFalse(r["ok"])
        self.assertEqual((await store.get_attempt(a["ad_id"]))["outcome"], "abandoned")

    async def test_too_fast_waits(self):
        db, mt, s = await self.grant_setup(started_ago=1.0)
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertTrue(r["wait"] >= 1)
        self.assertEqual((await fresh(db))["ads_done"], 0)

    async def test_wrong_user_or_session_cannot_claim(self):
        db, mt, s = await self.grant_setup()
        a = await service.start(s, 42)
        thief = await new_session(db, sid="S2", user_id=666)
        self.assertFalse((await service.done(thief, a["ad_id"], True))["ok"])
        self.assertFalse((await service.done(await fresh(db), "nonexistent", True))["ok"])
        self.assertFalse((await service.done(await fresh(db), None, True))["ok"])

    async def test_adsgram_requires_reward_url_hit(self):
        db, mt, s = await self.grant_setup(adsgram_secret=True)
        await configure(dict(BOTH, mode="manual", manual_network="adsgram"))
        a = await service.start(s, 42)
        self.assertEqual(a["network"], "adsgram")
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertEqual(r["wait"], 2)                                               # client says done, server has no hit
        await store.add_reward_hit(42)
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertEqual((r["granted"], r["trust"]), (True, "server_reward_url"))

    async def test_adsgram_hit_is_single_use_user_bound_and_time_bound(self):
        db, mt, s = await self.grant_setup(adsgram_secret=True)
        await configure(dict(BOTH, mode="manual", manual_network="adsgram"))
        await store.add_reward_hit(43)                                               # another user's hit
        await db["ad_reward_hits"].insert_one({"user_id": 42, "t": time.time() - 3600, "consumed_by": None})   # old hit
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertEqual(r.get("wait"), 2)                                           # neither counts
        await store.add_reward_hit(42)
        self.assertTrue((await service.done(await fresh(db), a["ad_id"], True))["granted"])
        # the same hit cannot pay for a second ad
        s2 = await new_session(db, sid="S3", user_id=42)
        b = await service.start(s2, 42)
        self.assertEqual((await service.done(await fresh(db, "S3"), b["ad_id"], True)).get("wait"), 2)

    async def test_adsgram_without_secret_is_client_attested_and_blocked_by_default(self):
        db, mt, s = await self.grant_setup()
        await configure({"networks": {"adsgram": {"enabled": True, "unit_id": "777"}}, "mode": "priority", "priority": ["adsgram", "monetag", "tads"]})
        plan = await engine.plan_request(random.Random(0))
        self.assertNotIn("adsgram", plan.order)                                      # not eligible => never even shown
        await configure({"networks": {"adsgram": {"allow_client_reward": True}}})
        self.assertIn("adsgram", (await engine.plan_request(random.Random(0))).order)

    async def test_tads_grant_is_labelled_client_attested(self):
        db, mt, s = await self.grant_setup()
        await configure(dict(BOTH, mode="manual", manual_network="tads"))
        a = await service.start(s, 42)
        self.assertEqual(a["network"], "tads")
        self.assertEqual((a["unit_id"], a["sdk"]), ("w1", settings.TADS_SDK_URL))
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertEqual((r["granted"], r["trust"]), (True, "client_attested"))
        self.assertEqual(list(db["ad_rewards"].docs.values())[0]["trust"], "client_attested")

    async def test_concurrent_done_grants_once(self):
        import asyncio
        db, mt, s = await self.grant_setup()
        a = await service.start(s, 42)
        sess = await fresh(db)
        rs = await asyncio.gather(*[service.done(sess, a["ad_id"], True) for _ in range(6)])
        self.assertEqual(sum(1 for r in rs if r.get("granted")), 1)
        self.assertEqual((await fresh(db))["ads_done"], 1)
        self.assertEqual(len(db["ad_rewards"].docs), 1)

    async def test_multi_ad_requirement(self):
        db, mt = setup_env()
        s = await new_session(db, required=2)
        a = await service.start(s, 42)
        r = await service.done(await fresh(db), a["ad_id"], True)
        self.assertEqual(r["s2"]["ads_done"], 1)
        b = await service.start(await fresh(db), 42)                                  # slot was released after verification
        self.assertTrue(b["ok"])
        self.assertNotEqual(a["ad_id"], b["ad_id"])


class StatsAndOptimizer(Base):
    async def test_rollups_count_each_outcome_once(self):
        db, mt = setup_env()
        await configure(dict(BOTH, mode="priority", priority=["adsgram", "tads", "monetag"]))
        s = await new_session(db)
        a = await service.start(s, 42)
        n = (await service.report(s, a["ad_id"], "fail", "no_fill"))["next"]
        await service.report(s, n["ad_id"], "shown")
        r = await service.done(await fresh(db), n["ad_id"], True)
        self.assertTrue(r["granted"])
        rows = {x["network"]: x for x in db["ad_stats_daily"].docs.values()}
        self.assertEqual((rows["adsgram"]["requests"], rows["adsgram"]["failed_nofill"]), (1, 1))
        self.assertEqual((rows["tads"]["requests"], rows["tads"]["shown"], rows["tads"]["completed"], rows["tads"]["verified"]), (1, 1, 1, 1))
        self.assertEqual(rows["tads"]["verified_client"], 1)

    async def test_tick_benches_failing_network_and_saves_scores(self):
        db, mt = setup_env()
        await configure(BOTH)
        day = store.day_key()
        b = int(time.time() // 600) * 600
        await db["ad_stats_daily"].insert_one({"_id": f"{day}|adsgram", "day": day, "network": "adsgram", "requests": 200, "shown": 20,
                                               "completed": 18, "verified": 18, "failed_nofill": 150, "failed_error": 10})
        await db["ad_stats_daily"].insert_one({"_id": f"{day}|monetag", "day": day, "network": "monetag", "requests": 300, "shown": 270,
                                               "completed": 250, "verified": 240})
        await db["ad_buckets"].insert_one({"_id": f"{b}|adsgram", "bucket": b, "network": "adsgram", "requests": 40, "hard_fails": 38})
        self.assertTrue(await engine.tick())
        plan = await store.get_doc("plan")
        self.assertIn("adsgram", plan["benched"])
        self.assertEqual(plan["leader"], "monetag")
        self.assertFalse(plan["scores"]["tads"]["measured"])
        self.assertFalse(await engine.tick())                                          # lease prevents a second concurrent run
        order = (await engine.plan_request(random.Random(3))).order
        self.assertNotIn("adsgram", order)                                             # cooling down

    async def test_expire_stale_attempts(self):
        db, mt = setup_env()
        s = await new_session(db)
        a = await service.start(s, 42)
        await db["ad_requests"].update_one({"_id": a["ad_id"]}, {"$set": {"t": time.time() - 9999}})
        self.assertEqual(await store.expire_stale(300), 1)
        self.assertEqual((await store.get_attempt(a["ad_id"]))["outcome"], "abandoned")
        self.assertIsNone((await fresh(db))["smart_active"])


if __name__ == "__main__":
    unittest.main()
