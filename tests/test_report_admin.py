import re
import time
import unittest

from ads import engine, migrate, panel, report, routes, service, settings, store
from ads.ratelimit import RateLimiter
from tests.fakemongo import FakeDB
from tests.helpers import configure, fresh, new_session, setup_env


class Revenue(unittest.IsolatedAsyncioTestCase):
    async def seed(self, db, net, day, **kw):
        await db["ad_stats_daily"].insert_one(dict({"_id": f"{day}|{net}", "day": day, "network": net}, **kw))

    async def test_confirmed_ecpm_uses_same_days_only(self):
        db, _ = setup_env()
        d0, d1 = store.day_key(0), store.day_key(1)
        await self.seed(db, "monetag", d0, requests=1200, shown=1000, completed=900, verified=800)
        await self.seed(db, "monetag", d1, requests=500, shown=400, completed=350, verified=300)   # no revenue entered for d1
        await store.set_confirmed_revenue(d0, "monetag", 4.0)
        rep = await report.build(7)
        m = rep["networks"]["monetag"]
        self.assertEqual(m["ecpm"]["confirmed_usd"], 5.0)            # 4.0 / 800 * 1000, d1's 300 impressions excluded
        self.assertEqual(m["revenue"]["confirmed_usd"], 4.0)
        self.assertEqual(m["revenue"]["basis"], "confirmed")
        self.assertEqual(rep["totals"]["overall_ecpm_confirmed_usd"], 5.0)
        self.assertEqual(rep["best"]["by_confirmed_ecpm"], "monetag")
        self.assertEqual(m["verified"], 1100)

    async def test_unavailable_revenue_is_none_never_zero_or_invented(self):
        db, _ = setup_env()
        await self.seed(db, "tads", store.day_key(), requests=50, shown=40, completed=30, verified=30)
        rep = await report.build(7)
        t = rep["networks"]["tads"]
        self.assertIsNone(t["revenue"]["confirmed_usd"])
        self.assertIsNone(t["ecpm"]["confirmed_usd"])
        self.assertIsNone(t["ecpm"]["estimated_usd"])
        self.assertEqual(t["revenue"]["basis"], "unavailable")
        self.assertIsNone(rep["totals"]["confirmed_revenue_usd"])
        self.assertIsNone(rep["best"]["by_confirmed_ecpm"])

    async def test_monetag_estimate_is_labelled_separately(self):
        db, _ = setup_env()
        d = store.day_key()
        await self.seed(db, "monetag", d, requests=250, shown=210, completed=200, verified=190)
        # window_rows uses aggregate(); the fake has none, so feed the estimate through the same seam the report uses
        async def est(days):
            return {d: {"revenue": 1.0, "impressions": 200}}
        orig, store.monetag_estimates = store.monetag_estimates, est
        try:
            m = (await report.build(7))["networks"]["monetag"]
        finally:
            store.monetag_estimates = orig
        self.assertEqual(m["ecpm"]["estimated_usd"], 5.0)
        self.assertIsNone(m["ecpm"]["confirmed_usd"])
        self.assertEqual(m["revenue"]["basis"], "estimated")

    async def test_summary_text_flags_estimates(self):
        setup_env()
        txt = report.summary_html(await report.build(7))
        self.assertIn("Estimated", txt)
        self.assertIn("confirmed", txt.lower())


class AdminApi(unittest.IsolatedAsyncioTestCase):
    def ctx(self, token="s3cret"):
        return routes.Ctx(auth=None, identify=None, send_file=None, db=None, token_re=re.compile("."), admin_token=token, reward_secret="rw")

    async def test_revenue_validation(self):
        setup_env()
        ok = await routes.h_admin_revenue({"day": store.day_key(), "network": "adsgram", "revenue_usd": 12.5})
        self.assertEqual(ok[0], 200)
        self.assertEqual((await store.revenue_rows())[0]["revenue_usd"], 12.5)
        for bad in ({"day": "2999-01-01", "network": "tads", "revenue_usd": 1}, {"day": "x", "network": "tads", "revenue_usd": 1},
                    {"day": store.day_key(), "network": "evil", "revenue_usd": 1}, {"day": store.day_key(), "network": "tads", "revenue_usd": -3},
                    {"day": store.day_key(), "network": "tads", "revenue_usd": "9"}, {"day": store.day_key(), "network": "tads", "revenue_usd": True},
                    {"day": store.day_key(), "network": "tads", "revenue_usd": float("nan")}, {"day": store.day_key(), "network": "tads", "revenue_usd": 1e9}):
            self.assertEqual((await routes.h_admin_revenue(bad))[0], 400, bad)

    async def test_config_update_persists_and_rejects(self):
        setup_env()
        st, out = await routes.h_admin_patch_config({"mode": "priority", "priority": ["tads", "adsgram", "monetag"]})
        self.assertEqual(st, 200)
        self.assertEqual((await routes.h_admin_config())["config"]["priority"][0], "tads")
        st, out = await routes.h_admin_patch_config({"networks": {"tads": {"enabled": True}}})     # no widget id anywhere
        self.assertEqual(st, 400)
        self.assertFalse((await engine.get_config(force=True))["networks"]["tads"]["enabled"])

    async def test_config_response_never_contains_secrets(self):
        setup_env(adsgram_secret=True)
        old = (settings.ADSGRAM_REWARD_SECRET, settings.ADS_ADMIN_TOKEN)
        settings.ADSGRAM_REWARD_SECRET, settings.ADS_ADMIN_TOKEN = "REWARDSECRET123", "ADMINTOKEN456"
        try:
            blob = repr(await routes.h_admin_config()) + repr(await report.build(7))
        finally:
            settings.ADSGRAM_REWARD_SECRET, settings.ADS_ADMIN_TOKEN = old
        self.assertNotIn("REWARDSECRET123", blob)
        self.assertNotIn("ADMINTOKEN456", blob)
        self.assertIn("adsgram_reward_secret", blob)                                # only the boolean flag is exposed
        html = open("ads/admin_ads.html", encoding="utf-8").read() + open("ads/adapters.js", encoding="utf-8").read()
        self.assertNotIn("REWARDSECRET123", html)

    def test_admin_auth_and_lockout(self):
        c = self.ctx()
        self.assertIsNone(routes.admin_check(c, "Bearer s3cret", "1.1.1.1"))
        self.assertEqual(routes.admin_check(c, "Bearer nope", "1.1.1.1")[0], 403)
        self.assertEqual(routes.admin_check(c, None, "1.1.1.1")[0], 403)
        self.assertEqual(routes.admin_check(self.ctx(""), "Bearer x", "1.1.1.1")[0], 404)       # disabled without a token
        c2 = self.ctx()
        codes = [routes.admin_check(c2, "Bearer bad", "9.9.9.9")[0] for _ in range(14)]
        self.assertEqual(codes[-1], 429)                                                          # brute force lock-out
        self.assertEqual(routes.admin_check(c2, "Bearer s3cret", "8.8.8.8"), None)                # other IPs unaffected

    async def test_adsgram_reward_endpoint(self):
        setup_env()
        c = self.ctx()
        self.assertEqual((await routes.h_adsgram_reward(c, {"secret": "bad", "userid": "5"}, "ip"))[0], 403)
        self.assertEqual((await routes.h_adsgram_reward(c, {"secret": "rw", "userid": "5; drop"}, "ip"))[0], 400)
        self.assertEqual((await routes.h_adsgram_reward(c, {"secret": "rw"}, "ip"))[0], 400)
        self.assertEqual(len(store._c("ad_reward_hits").docs), 0)                    # invalid calls store nothing
        self.assertEqual((await routes.h_adsgram_reward(c, {"secret": "rw", "userid": "5"}, "ip"))[0], 200)
        self.assertEqual(len(store._c("ad_reward_hits").docs), 1)
        c.reward_secret = ""
        self.assertEqual((await routes.h_adsgram_reward(c, {"secret": "", "userid": "5"}, "ip"))[0], 404)

    def test_rate_limiter(self):
        t = [0.0]
        rl = RateLimiter(3, 10, clock=lambda: t[0])
        self.assertEqual([rl.allow("k") for _ in range(4)], [True, True, True, False])
        t[0] = 11
        self.assertTrue(rl.allow("k"))


class Handlers(unittest.IsolatedAsyncioTestCase):
    """h_start / h_report / h_done wiring incl. the file-delivery tail, with stubbed Telegram bits."""
    async def make(self, required=1):
        db, mt = setup_env(ads_required=required)
        s = await new_session(db, required=required)
        sent = []

        class Bot:
            async def delete_message(self, *a): pass

        class App:
            bot = Bot()

        async def auth(body):
            sess = await fresh(db, body.get("sid") or "")
            if not sess or body.get("init_data") != "ok":
                return None, None, "Verification failed. Please open the link again."
            return sess, App(), None

        class DBShim:
            async def get_file(self, t): return {"token": t}
            async def claim_delivery(self_, sid):
                r = await db["sessions"].find_one_and_update({"sid": sid, "delivered": False, "ads_done": {"$gte": 1}}, {"$set": {"delivered": True}})
                return r is not None
            async def unclaim_delivery(self_, sid): await db["sessions"].update_one({"sid": sid}, {"$set": {"delivered": False}})

        async def send_file(bot, chat, doc, mgr):
            sent.append(doc["token"]); return True

        ctx = routes.Ctx(auth=auth, identify=None, send_file=send_file, db=DBShim(), token_re=re.compile("."), ads_required=required)
        return db, ctx, sent

    async def test_full_http_flow_delivers_once(self):
        db, ctx, sent = await self.make()
        body = {"sid": "S1", "init_data": "ok"}
        st = await routes.h_start(ctx, body, "ip")
        self.assertTrue(st["ok"])
        await routes.h_report(ctx, dict(body, ad_id=st["ad_id"], event="shown"), "ip")
        r = await routes.h_done(ctx, dict(body, ad_id=st["ad_id"], completed=True), "ip")
        self.assertEqual((r["ok"], r["delivered"]), (True, True))
        self.assertEqual(sent, ["tok"])
        again = await routes.h_done(ctx, dict(body, ad_id=st["ad_id"], completed=True), "ip")
        self.assertFalse(again["ok"])
        self.assertEqual(sent, ["tok"])                                              # no second delivery

    async def test_auth_failure_and_input_validation(self):
        db, ctx, sent = await self.make()
        self.assertFalse((await routes.h_start(ctx, {"sid": "S1", "init_data": "forged"}, "ip"))["ok"])
        self.assertFalse((await routes.h_done(ctx, {"sid": "S1", "init_data": "forged", "ad_id": "x", "completed": True}, "ip"))["ok"])
        self.assertFalse((await routes.h_report(ctx, {"sid": "S1", "init_data": "ok", "event": "grant"}, "ip"))["ok"])
        st = await routes.h_start(ctx, {"sid": "S1", "init_data": "ok"}, "ip")
        # "completed": "true" (string) is not the boolean True => treated as not completed
        r = await routes.h_done(ctx, {"sid": "S1", "init_data": "ok", "ad_id": st["ad_id"], "completed": "true"}, "ip")
        self.assertFalse(r["ok"])
        self.assertEqual(sent, [])

    async def test_client_cannot_inject_revenue_or_trust(self):
        db, ctx, sent = await self.make()
        st = await routes.h_start(ctx, {"sid": "S1", "init_data": "ok"}, "ip")
        await routes.h_done(ctx, {"sid": "S1", "init_data": "ok", "ad_id": st["ad_id"], "completed": True, "revenue": 99, "trust": "server_postback",
                                  "network": "adsgram"}, "ip")
        rew = list(db["ad_rewards"].docs.values())[0]
        self.assertEqual((rew["network"], rew["trust"]), ("monetag", "client_attested"))
        self.assertEqual(len(await store.revenue_rows()), 0)

    async def test_rate_limit_on_start(self):
        db, ctx, sent = await self.make()
        ctx.rl_user = RateLimiter(2, 60)
        body = {"sid": "S1", "init_data": "ok"}
        res = [await routes.h_start(ctx, body, f"ip{i}") for i in range(4)]
        self.assertEqual(res[-1].get("code"), "rate_limited")


class RegisterAndPanel(unittest.IsolatedAsyncioTestCase):
    def test_register_mounts_expected_routes(self):
        from tests.stubs import fastapi_stub
        fastapi_stub.install()
        app = fastapi_stub.FastAPI()
        db, mt = setup_env()
        old = (settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET)
        settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET = "tok", "sec"
        try:
            from ads.verify import Policy
            routes.register(app, auth=None, identify=None, send_file=None, db=None, token_re=re.compile("."), telegram_errors=(), forbidden=(),
                            policy=Policy(), manager=None, ads_required=1)
        finally:
            settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET = old
        got = set(app.routes)
        for r in [("POST", "/api/smart/start"), ("POST", "/api/smart/report"), ("POST", "/api/smart/done"), ("GET", "/ads/adapters.js"),
                  ("GET", "/api/adsgram/reward"), ("GET", "/admin/ads"), ("GET", "/admin/ads/api/overview"), ("POST", "/admin/ads/api/config"),
                  ("POST", "/admin/ads/api/revenue")]:
            self.assertIn(r, got)

    def test_register_without_tokens_exposes_no_admin_or_reward_routes(self):
        from tests.stubs import fastapi_stub
        fastapi_stub.install()
        app = fastapi_stub.FastAPI()
        setup_env()
        old = (settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET)
        settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET = "", ""
        try:
            from ads.verify import Policy
            routes.register(app, auth=None, identify=None, send_file=None, db=None, token_re=re.compile("."), telegram_errors=(), forbidden=(),
                            policy=Policy(), manager=None, ads_required=1)
        finally:
            settings.ADS_ADMIN_TOKEN, settings.ADSGRAM_REWARD_SECRET = old
        self.assertFalse([p for (_m, p) in app.routes if p.startswith("/admin") or "adsgram" in p])

    async def test_panel_toggle_and_rejections(self):
        setup_env()
        self.assertIsNone(await panel.apply("sa:t:monetag"))
        self.assertFalse((await engine.get_config(force=True))["networks"]["monetag"]["enabled"])
        self.assertIn("cannot be enabled", await panel.apply("sa:t:tads"))                # no widget id => refused, not silently enabled
        self.assertIsNone(await panel.apply("sa:m:priority"))
        self.assertEqual((await engine.get_config(force=True))["mode"], "priority")
        self.assertIsNone(await panel.apply("sa:fb"))
        self.assertFalse((await engine.get_config(force=True))["fallback_enabled"])
        self.assertIsNone(await panel.apply("sa:t:../../etc"))                              # junk is ignored
        text, rows = await panel.render_home("https://x/admin/ads")
        self.assertIn("Smart Ads", text)
        self.assertTrue(any(u for row in rows for (_l, _d, u) in row))

    async def test_migration_is_idempotent_and_touches_only_new_collections(self):
        db = FakeDB()
        await db["files"].insert_one({"_id": "f", "token": "t"})
        await migrate.run(db)
        await migrate.run(db)
        self.assertEqual(db["ad_config"].docs["migration"]["version"], 1)
        self.assertEqual(list(db["files"].docs), ["f"])
        self.assertTrue(all(n.startswith("ad_") or n == "files" for n in db.cols))      # nothing else was created


if __name__ == "__main__":
    unittest.main()
