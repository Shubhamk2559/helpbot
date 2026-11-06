// Run: node --test tests/adapters.test.js     (mocked SDKs only; no real network, no real ad network)
const test = require("node:test");
const assert = require("node:assert/strict");
const SA = require("../ads/adapters.js");

function makeEnv(win, opts = {}) {
  const clock = { t: 0 };
  const env = {
    win, clock,
    now: () => clock.t,
    // polling timers advance the virtual clock; long watchdogs are scaled down (10s -> 10ms, 90s -> 90ms)
    setTimeout: (f, ms) => ms <= 200 ? setTimeout(() => { clock.t += ms; f(); }, 0) : setTimeout(f, Math.max(1, ms / 1000)),
    clearTimeout: (t) => clearTimeout(t),
    addScript: (url, attrs, ok, err) => {
      env.scripts = (env.scripts || []).concat([url]);
      if (opts.blockScripts) return setTimeout(err, 0);
      setTimeout(() => { if (opts.onScript) opts.onScript(win); ok(); }, 0);
    },
    ensureContainer: (id) => { env.containers = (env.containers || []).concat([id]); },
  };
  return env;
}
const hooksRec = () => { const h = { shown: 0, onShown() { h.shown++; } }; return h; };

// ---------------------------------------------------------------- Monetag
test("monetag: valued completion", async () => {
  const win = { show_111: async (o) => { assert.equal(o.ymid, "Y1"); assert.equal(o.requestVar, "unlock"); return { reward_event_type: "valued" }; } };
  const h = hooksRec();
  const r = await SA.createAdapters(makeEnv(win)).monetag.run({ zone: "111", ymid: "Y1", sdk: "https://s/111.js" }, h);
  assert.deepEqual([r.status, r.nonValued, r.shown, h.shown], ["completed", false, true, 1]);
});
test("monetag: non_valued is surfaced (server then denies)", async () => {
  const win = { show_111: async () => ({ reward_event_type: "non_valued" }) };
  const r = await SA.createAdapters(makeEnv(win)).monetag.run({ zone: "111", ymid: "Y" }, hooksRec());
  assert.equal(r.nonValued, true);
});
test("monetag: empty feed quickly => no_fill, never displayed", async () => {
  const win = { show_111: async () => { throw new Error("Empty feed"); } };
  const h = hooksRec();
  const r = await SA.createAdapters(makeEnv(win)).monetag.run({ zone: "111", ymid: "Y" }, h);
  assert.deepEqual([r.status, r.reason, r.shown, h.shown], ["failed", "no_fill", false, 0]);
});
test("monetag: rejection after the ad had time to display counts as displayed (no second ad)", async () => {
  const env = makeEnv({});
  env.win.show_111 = async () => { env.clock.t += 5000; throw new Error("boom"); };
  const h = hooksRec();
  const r = await SA.createAdapters(env).monetag.run({ zone: "111", ymid: "Y" }, h);
  assert.deepEqual([r.status, r.shown, h.shown], ["failed", true, 1]);
});
test("monetag: blocked script", async () => {
  const r = await SA.createAdapters(makeEnv({}, { blockScripts: true })).monetag.run({ zone: "111", ymid: "Y", sdk: "https://s/x.js" }, hooksRec());
  assert.deepEqual([r.status, r.reason, r.shown], ["failed", "blocked", false]);
});
test("monetag: SDK loads and exposes show_ZONE", async () => {
  const env = makeEnv({}, { onScript: (w) => { w.show_9 = async () => ({ reward_event_type: "valued" }); } });
  const r = await SA.createAdapters(env).monetag.run({ zone: "9", ymid: "Y", sdk: "https://s/9.js" }, hooksRec());
  assert.equal(r.status, "completed");
  assert.deepEqual(env.scripts, ["https://s/9.js"]);
});

// ---------------------------------------------------------------- AdsGram
function adsgramWin(script) {
  const listeners = {};
  const ctl = {
    addEventListener: (e, f) => { (listeners[e] = listeners[e] || []).push(f); },
    removeEventListener: (e, f) => { listeners[e] = (listeners[e] || []).filter((x) => x !== f); },
    emit: (e) => (listeners[e] || []).forEach((f) => f()),
    show: () => script(ctl),
  };
  const win = { Adsgram: { init: (o) => { win.initArgs = o; return ctl; } } };
  return { win, ctl, listeners };
}
const AG = { unit_id: 123, sdk: "https://sad/x.js", debug: false };

test("adsgram: rewarded completion", async () => {
  const { win, ctl } = adsgramWin(async (c) => { c.emit("onStart"); c.emit("onReward"); return { done: true, state: "destroy", error: false, description: "" }; });
  const h = hooksRec();
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, h);
  assert.deepEqual([r.status, r.shown, h.shown], ["completed", true, 1]);
  assert.deepEqual(win.initArgs, { blockId: "123", debug: false });
});
test("adsgram: listeners removed after each run (controller is shared per blockId)", async () => {
  const { win, listeners } = adsgramWin(async (c) => { c.emit("onReward"); return { done: true }; });
  const a = SA.createAdapters(makeEnv(win)).adsgram;
  await a.run(AG, hooksRec()); await a.run(AG, hooksRec());
  assert.ok(Object.values(listeners).every((l) => l.length === 0));
});
test("adsgram: interstitial block resolves on close but is NOT a reward", async () => {
  const { win } = adsgramWin(async (c) => { c.emit("onStart"); c.emit("onComplete"); return { done: true }; });
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.detail], ["failed", "block_not_rewarded"]);
});
test("adsgram: skip is skipped, not completed", async () => {
  const { win } = adsgramWin((c) => { c.emit("onStart"); c.emit("onSkip"); return Promise.reject({ done: false, state: "playing", error: false, description: "skip" }); });
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.shown], ["skipped", true]);
});
test("adsgram: no banner => no_fill and nothing displayed", async () => {
  const { win } = adsgramWin((c) => { c.emit("onBannerNotFound"); return Promise.reject({ done: false, state: "load", error: true }); });
  const h = hooksRec();
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, h);
  assert.deepEqual([r.status, r.reason, r.shown, h.shown], ["failed", "no_fill", false, 0]);
});
test("adsgram: render error before display is eligible for fallback, error while playing is not", async () => {
  let w = adsgramWin(() => Promise.reject({ done: false, state: "render", error: true }));
  let r = await SA.createAdapters(makeEnv(w.win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.shown], ["failed", false]);
  w = adsgramWin(() => Promise.reject({ done: false, state: "playing", error: true }));
  r = await SA.createAdapters(makeEnv(w.win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.shown], ["failed", true]);
});
test("adsgram: throttling events are errors, never rewards", async () => {
  const { win } = adsgramWin((c) => { c.emit("onNonStopShow"); return Promise.reject({ done: false, state: "load", error: true }); });
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.detail], ["failed", "throttled"]);
});
test("adsgram: resolved but done=false is not completion", async () => {
  const { win } = adsgramWin(async () => ({ done: false }));
  assert.equal((await SA.createAdapters(makeEnv(win)).adsgram.run(AG, hooksRec())).status, "failed");
});
test("adsgram: hung ad times out", async () => {
  const { win } = adsgramWin(() => new Promise(() => {}));
  const r = await SA.createAdapters(makeEnv(win)).adsgram.run(AG, hooksRec());
  assert.deepEqual([r.status, r.reason], ["failed", "timeout"]);
});

// ---------------------------------------------------------------- Tads
function tadsWin(onShow) {
  const win = { tads: { init: (cfg) => { win.cfg = cfg; return { showAd: () => onShow(win.cfg) }; } } };
  return win;
}
const TD = { unit_id: "w7", sdk: "https://w.tads.me/widget.js", debug: false };

test("tads: onShowReward after showAd => completed (client-attested); init uses widgetId", async () => {
  const win = tadsWin(async (cfg) => { setTimeout(() => cfg.onShowReward("ad-9"), 5); });
  const env = makeEnv(win);
  const h = hooksRec();
  const r = await SA.createAdapters(env).tads.run(TD, h);
  assert.deepEqual([r.status, r.adId, r.shown, h.shown], ["completed", "ad-9", true, 1]);
  assert.equal(win.cfg.widgetId, "w7");
  assert.deepEqual(env.containers, ["tads-container-w7"]);
});
test("tads: a CLICK is never completion", async () => {
  const win = tadsWin(async (cfg) => { setTimeout(() => cfg.onClickReward("ad-1"), 5); });
  const r = await SA.createAdapters(makeEnv(win)).tads.run(TD, hooksRec());
  assert.deepEqual([r.status, r.reason], ["failed", "timeout"]);
});
test("tads: onAdsNotFound => no_fill and never reported as displayed", async () => {
  const win = tadsWin(async (cfg) => { cfg.onAdsNotFound(); });
  const h = hooksRec();
  const r = await SA.createAdapters(makeEnv(win)).tads.run(TD, h);
  assert.deepEqual([r.status, r.reason, r.shown, h.shown], ["failed", "no_fill", false, 0]);
});
test("tads: showAd rejection => error before display", async () => {
  const win = tadsWin(() => Promise.reject(new Error("x")));
  const r = await SA.createAdapters(makeEnv(win)).tads.run(TD, hooksRec());
  assert.deepEqual([r.status, r.reason, r.shown], ["failed", "error", false]);
});
test("tads: controller is created once and callbacks follow the current run", async () => {
  let inits = 0;
  const win = { tads: { init: (cfg) => { inits++; win.cfg = cfg; return { showAd: async () => setTimeout(() => win.cfg.onShowReward("a"), 3) }; } } };
  const t = SA.createAdapters(makeEnv(win)).tads;
  await t.run(TD, hooksRec()); await t.run(TD, hooksRec());
  assert.equal(inits, 1);
});

// ---------------------------------------------------------------- orchestrator
function fakeApi(script) {
  const calls = [];
  const api = async (path, body) => { calls.push([path, body.event || null, body.ad_id]); return script(path, body) || {}; };
  return { api, calls };
}
const A = (name, results) => ({ name, run: async (cfg, hooks) => { const r = results.shift(); if (r.shown) hooks.onShown(); return r; } });

test("chain: falls back only when server confirms nothing was displayed", async () => {
  const adapters = { adsgram: A("adsgram", [{ status: "failed", reason: "no_fill", shown: false }]), monetag: A("monetag", [{ status: "completed", shown: true }]) };
  const { api, calls } = fakeApi((p, b) => (b.event === "fail" ? { ok: true, next: { ok: true, network: "monetag", ad_id: "B" } } : {}));
  const out = await SA.runChain({ network: "adsgram", ad_id: "A" }, adapters, api, {});
  assert.deepEqual([out.kind, out.state.ad_id], ["completed", "B"]);
  assert.deepEqual(calls.map((c) => c[1]), ["shown", "fail"].slice(1).concat(["shown"]));
});
test("chain: after a displayed ad fails, no second ad is attempted", async () => {
  let second = 0;
  const adapters = { adsgram: A("adsgram", [{ status: "failed", reason: "error", shown: true }]), monetag: { run: async () => { second++; return { status: "completed" }; } } };
  const { api, calls } = fakeApi(() => ({ ok: true, exhausted: true, displayed: true }));
  const out = await SA.runChain({ network: "adsgram", ad_id: "A" }, adapters, api, {});
  assert.deepEqual([out.kind, out.displayed, second], ["exhausted", true, 0]);
  assert.deepEqual(calls.map((c) => c[1]), ["shown", "fail"]);                // 'shown' is reported (and awaited) BEFORE 'fail'
});
test("chain: server refusing a fallback ends the chain", async () => {
  const adapters = { tads: A("tads", [{ status: "failed", reason: "no_fill", shown: false }]) };
  const { api } = fakeApi(() => ({ ok: true, exhausted: true }));
  assert.equal((await SA.runChain({ network: "tads", ad_id: "A" }, adapters, api, {})).kind, "exhausted");
});
test("chain: skip is reported and stops", async () => {
  const adapters = { adsgram: A("adsgram", [{ status: "skipped", shown: true }]) };
  const { api, calls } = fakeApi(() => ({}));
  assert.equal((await SA.runChain({ network: "adsgram", ad_id: "A" }, adapters, api, {})).kind, "skipped");
  assert.ok(calls.some((c) => c[1] === "skip"));
});
test("chain: adapter exception is treated as a failure, never completion", async () => {
  const adapters = { tads: { run: async () => { throw new Error("sdk exploded"); } } };
  const { api } = fakeApi(() => ({ ok: true, exhausted: true }));
  assert.equal((await SA.runChain({ network: "tads", ad_id: "A" }, adapters, api, {})).kind, "exhausted");
});
test("claim: polls while the server asks to wait, forwards only completed + non_valued", async () => {
  let n = 0; const bodies = [];
  const api = async (p, b) => { bodies.push(b); return ++n < 3 ? { ok: true, wait: 1 } : { ok: true, delivered: true }; };
  const r = await SA.claim({ ad_id: "A" }, { nonValued: false, revenue: 99, trust: "x" }, api, async () => {});
  assert.equal(r.delivered, true);
  assert.deepEqual(Object.keys(bodies[0]).sort(), ["ad_id", "completed", "non_valued"]);
});
