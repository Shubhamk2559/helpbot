import random
import unittest

from ads import scoring, settings, verify
from ads.scoring import NetStats, Params
from ads.verify import Facts, Policy, decide


def st(n, req=0, shown=0, comp=0, ver=0, err=0, nofill=0, rev=0.0, creq=0, cver=0):
    return NetStats(n, requests=req, shown=shown, completed=comp, verified=ver, failed_error=err, failed_nofill=nofill,
                    conf_revenue=rev, conf_requests=creq, conf_verified=cver)


class Scoring(unittest.TestCase):
    P = Params(min_requests=100, min_rev_imp=50)

    def test_unmeasured_network_is_never_scored(self):
        sc = scoring.compute_scores({"monetag": st("monetag", 500, 450, 400, 390), "adsgram": st("adsgram", 5, 5, 5, 5)}, {}, self.P)
        self.assertTrue(sc["monetag"]["measured"])
        self.assertFalse(sc["adsgram"]["measured"])
        self.assertIsNone(sc["adsgram"]["score"])

    def test_revenue_ignored_when_only_one_network_has_confirmed_revenue(self):
        w = {"monetag": st("monetag", 500, 450, 400, 390, rev=5, creq=500, cver=390),
             "adsgram": st("adsgram", 500, 300, 250, 240)}
        sc = scoring.compute_scores(w, {}, self.P)
        self.assertIsNone(sc["monetag"]["components"]["revenue"])
        self.assertIsNone(sc["monetag"]["revenue_basis"])

    def test_higher_confirmed_revenue_wins_when_reliability_equal(self):
        same = dict(req=1000, shown=900, comp=800, ver=780)
        w = {"monetag": st("monetag", rev=2.0, creq=1000, cver=780, **same), "adsgram": st("adsgram", rev=6.0, creq=1000, cver=780, **same),
             "tads": st("tads", **same)}
        sc = scoring.compute_scores(w, {}, self.P)
        self.assertGreater(sc["adsgram"]["score"], sc["monetag"]["score"])
        self.assertEqual(sc["adsgram"]["revenue_basis"], "confirmed")
        self.assertIsNone(sc["tads"]["revenue_basis"])             # unknown revenue is treated as average, never invented

    def test_small_revenue_sample_is_shrunk_not_trusted(self):
        # 30 impressions < min_rev_imp(50): the 'great' revenue is not used at all
        w = {"monetag": st("monetag", 1000, 900, 800, 780, rev=1, creq=1000, cver=780),
             "adsgram": st("adsgram", 1000, 900, 800, 780, rev=500, creq=30, cver=30)}
        self.assertIsNone(scoring.compute_scores(w, {}, self.P)["adsgram"]["components"]["revenue"])

    def test_errors_reduce_score(self):
        w = {"monetag": st("monetag", 1000, 900, 800, 780, err=10), "adsgram": st("adsgram", 1000, 900, 800, 780, err=400)}
        sc = scoring.compute_scores(w, {}, self.P)
        self.assertGreater(sc["monetag"]["score"], sc["adsgram"]["score"])

    def test_choose_order_cold_start_follows_priority_but_explores(self):
        counts = {"monetag": 0, "adsgram": 0, "tads": 0}
        rng = random.Random(1)
        for _ in range(3000):
            order, w, _ = scoring.choose_order(["monetag", "adsgram", "tads"], {}, ["monetag", "adsgram", "tads"], 0.15, rng)
            counts[order[0]] += 1
            self.assertEqual(sorted(order), ["adsgram", "monetag", "tads"])      # always a full fallback chain
        self.assertGreater(counts["monetag"], counts["adsgram"])
        self.assertGreater(counts["adsgram"], counts["tads"])
        self.assertTrue(all(c > 100 for c in counts.values()))

    def test_choose_order_leader_gets_most_traffic_but_others_keep_floor(self):
        scores = {"monetag": {"measured": True, "score": 0.9}, "adsgram": {"measured": True, "score": 0.5}, "tads": {"measured": False, "score": None}}
        _o, w, _ = scoring.choose_order(["monetag", "adsgram", "tads"], scores, ["monetag", "adsgram", "tads"], 0.2, random.Random(0))
        self.assertAlmostEqual(w["monetag"], 0.8)
        self.assertGreater(w["tads"], w["adsgram"])                  # unmeasured explored faster
        self.assertAlmostEqual(sum(w.values()), 1.0, places=5)

    def test_single_and_empty(self):
        self.assertEqual(scoring.choose_order(["tads"], {}, ["tads"], 0.1)[0], ["tads"])
        self.assertEqual(scoring.choose_order([], {}, [], 0.1)[0], [])

    def test_find_bench(self):
        self.assertEqual(scoring.find_bench({"a": (20, 18), "b": (20, 2), "c": (3, 3)}, 15, 0.7), {"a"})


class Verify(unittest.TestCase):
    POL = Policy(monetag_postback=True, adsgram_reward_secret=True, min_seconds=5, wait_seconds=20)
    LEG = Policy(monetag_postback=False, adsgram_reward_secret=False, min_seconds=5, wait_seconds=20)

    def f(self, net, **kw):
        base = dict(network=net, elapsed=30, client_completed=True, claim_age=1)
        base.update(kw)
        return Facts(**base)

    def test_client_flag_alone_never_grants_when_server_signal_exists(self):
        self.assertEqual(decide(self.f("monetag"), self.POL, True).action, "wait")
        self.assertEqual(decide(self.f("adsgram"), self.POL, True).action, "wait")

    def test_deny_after_wait_budget(self):
        self.assertEqual(decide(self.f("monetag", claim_age=25), self.POL, True).action, "deny")
        self.assertEqual(decide(self.f("adsgram", claim_age=25), self.POL, True).reason, "unverified")

    def test_monetag_postback_valued_grants_with_server_trust(self):
        d = decide(self.f("monetag", postback_seen=True, postback_valued=True), self.POL, False)
        self.assertEqual((d.action, d.trust), ("grant", "server_postback"))

    def test_monetag_non_valued_denied(self):
        self.assertEqual(decide(self.f("monetag", postback_seen=True, postback_valued=False), self.POL, True).reason, "non_valued")
        self.assertEqual(decide(self.f("monetag", client_non_valued=True), self.LEG, True).reason, "non_valued")

    def test_adsgram_reward_hit_grants(self):
        d = decide(self.f("adsgram", reward_hit=True), self.POL, False)
        self.assertEqual((d.action, d.trust), ("grant", "server_reward_url"))

    def test_client_attested_requires_explicit_permission(self):
        self.assertEqual(decide(self.f("tads"), self.LEG, False).reason, "client_attested_not_allowed")
        d = decide(self.f("tads"), self.LEG, True)
        self.assertEqual((d.action, d.trust), ("grant", "client_attested"))
        self.assertEqual(decide(self.f("adsgram"), self.LEG, False).action, "deny")

    def test_not_completed_and_too_fast(self):
        self.assertEqual(decide(self.f("tads", client_completed=False), self.LEG, True).reason, "not_completed")
        d = decide(self.f("tads", elapsed=1), self.LEG, True)
        self.assertEqual((d.action, d.wait), ("wait", 4))

    def test_unknown_network(self):
        self.assertEqual(decide(self.f("evil"), self.LEG, True).action, "deny")


class Settings(unittest.TestCase):
    def setUp(self):
        self.cfg = settings.merged(None)

    def test_defaults_keep_legacy_behaviour(self):
        self.assertTrue(self.cfg["networks"]["monetag"]["enabled"])
        self.assertTrue(self.cfg["networks"]["monetag"]["allow_client_reward"])
        self.assertFalse(self.cfg["networks"]["adsgram"]["enabled"])
        self.assertFalse(self.cfg["networks"]["tads"]["allow_client_reward"])

    def test_cannot_enable_unconfigured_network(self):
        _c, e = settings.validate_patch({"networks": {"adsgram": {"enabled": True}}}, self.cfg)
        self.assertTrue(e)
        new, e = settings.validate_patch({"networks": {"adsgram": {"enabled": True, "unit_id": "12345"}}}, self.cfg)
        self.assertFalse(e)
        self.assertTrue(new["networks"]["adsgram"]["enabled"])

    def test_rejects_bad_values_atomically(self):
        for bad in ({"mode": "x"}, {"priority": ["monetag"]}, {"max_attempts": 9}, {"networks": {"adsgram": {"unit_id": "abc"}}},
                    {"optimization": {"explore_share": 0.9}}, {"optimization": {"weights": {"revenue": 5}}}, {"nope": 1},
                    {"manual_network": "zzz"}, {"mode": "manual"}, {"fallback_enabled": "yes"}):
            new, e = settings.validate_patch(bad, self.cfg)
            self.assertTrue(e, bad)
            self.assertEqual(new, self.cfg)

    def test_valid_patch(self):
        new, e = settings.validate_patch({"mode": "manual", "manual_network": "monetag", "priority": ["tads", "adsgram", "monetag"],
                                          "optimization": {"min_requests": 50, "weights": {"revenue": 0.3}}}, self.cfg)
        self.assertFalse(e)
        self.assertEqual(new["priority"][0], "tads")
        self.assertEqual(new["optimization"]["min_requests"], 50)
        self.assertEqual(new["optimization"]["weights"]["revenue"], 0.3)

    def test_trust_mapping(self):
        settings.RUNTIME.update(monetag_postback=False, adsgram_secret=False)
        self.assertEqual(settings.trust_for("tads"), "client_attested")
        self.assertEqual(settings.trust_for("monetag"), "client_attested")
        settings.RUNTIME.update(monetag_postback=True, adsgram_secret=True)
        self.assertEqual(settings.trust_for("monetag"), "server_postback")
        self.assertEqual(settings.trust_for("adsgram"), "server_reward_url")
        settings.RUNTIME.update(monetag_postback=False, adsgram_secret=False)


if __name__ == "__main__":
    unittest.main()
