#!/usr/bin/env python3
"""
Konto-Radar  -  opsporing af nye / mistænkelige konti på lavere tier CS2 (Polymarket)

Botten kigger på ALLE bets på CS2-kampe (undtagen tier 1) og melder i Discord, når:
  💰 STORT BET        én handel på mindst BIG_BET_USD
  🧩 MANGE SMÅ BETS   en konto har satset mindst MANY_SMALL_USD på én kamp, fordelt på
                      mindst MANY_SMALL_COUNT bets (meldes igen ved højere trin)
  📈 SAMLET AKTIVITET en konto har satset mindst TOTAL_USD på lavere tier CS2 i alt
  🆕 NY KONTO         en konto med få handler i alt sætter mindst NEW_MIN_USD på en kamp

Hver melding får en mistankescore (ny konto, underdog-pris, mistænkt hold, før kampstart,
stor del af kontoens samlede omsætning ...). Konti der ser interessante ud kan I selv
tilføje til Sussytracker v2 - linjen til det står nederst i hver besked.
"""
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# ---------------- Indstillinger ----------------
BIG_BET_USD = 500          # 💰 én handel på mindst så meget
MANY_SMALL_USD = 500       # 🧩 samlet på én kamp ...
MANY_SMALL_COUNT = 3       #    ... fordelt på mindst så mange bets
MANY_SMALL_STEPS = [500, 1000, 2000, 5000, 10000]
TOTAL_USD = 1000           # 📈 samlet på lavere tier CS2 (alle kampe i vinduet)
TOTAL_COUNT = 5
TOTAL_STEPS = [1000, 2500, 5000, 10000, 25000]
NEW_MAX_TRADES = 25        # 🆕 "ny konto" = højst så mange handler i alt på Polymarket
NEW_MIN_USD = 150          #    ... og mindst så meget på én kamp
LOOKUP_MIN_USD = 100       # konti under dette på en kamp slås ikke op (sparer kald)
PRO_MIN_TRADES = 5000      # meget aktive konti (market makers/pro'er) springes over ...
PRO_SKIP_BELOW_USD = 2500  # ... medmindre de satser mindst så meget på én kamp
MIN_TRADE_USD = 5          # handler under dette ignoreres helt (støv)

HOURS_BEFORE = 48          # kampe der starter inden for så mange timer
HOURS_AFTER = 6            # ... eller startede for højst så mange timer siden
MAX_EVENT_VOLUME = 150000  # større kampe = top-kampe -> springes over
TIER1_KEYWORDS = [
    "major", "iem ", "intel extreme masters", "blast", "esl pro league", "pgl ",
    "esports world cup", "ewc ", "starladder", "thunderpick world", "fissure",
    "betboom dacha", "perfect world",
]
# Mistænkte hold ligger i den hemmelige indstilling SUSPECT_TEAMS (ét hold pr. linje),
# så listen ikke står offentligt her. De markeres med ⚠️ og giver ekstra score.
SUSPECT_TEAMS = [t.strip() for t in re.split(r"[\n,]", os.environ.get("SUSPECT_TEAMS") or "") if t.strip()]

SECONDS_BETWEEN = int(os.environ.get("SECONDS_BETWEEN") or 60)
RUN_SECONDS = int(os.environ.get("RUN_SECONDS") or 0)       # 0 = kør én runde
RESCAN_EVERY = 300                                         # find nye kampe hvert 5. min
MAX_PAGES = 5                                              # 5 x 1000 handler pr. 20 kampe
MAX_ALERTS_PER_ROUND = 15
STATE_FILE = "state.json"
MAX_STATE = 20000
# -----------------------------------------------

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_RADAR", "").strip()
STATUS_HOOK = os.environ.get("DISCORD_WEBHOOK_STATUS", "").strip()
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
KNOWN = {l.split()[0].lower() for l in (os.environ.get("KNOWN_WALLETS") or "").splitlines()
         if l.strip().lower().startswith("0x")}

session = requests.Session()
session.headers["User-Agent"] = "konto-radar/1.0"
wallet_cache = {}  # wallet -> (tid, info)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


SUSPECT = {norm(t): t for t in SUSPECT_TEAMS}


# ---------- hjælpefunktioner ----------
def get(url, params=None):
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(int(r.headers.get("Retry-After") or 3 * (attempt + 1)), 30))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"Fejl ved {url}: {e}")
            time.sleep(2)
    return None


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def short(w):
    return f"{w[:6]}…{w[-4:]}"


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("alerted", [])
    return s


def save_state(state):
    state["alerted"] = state["alerted"][-MAX_STATE:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def post(payload, ping=False):
    if ping and ROLE_ID:
        payload["content"] = f"<@&{ROLE_ID}> " + payload.get("content", "")
        payload["allowed_mentions"] = {"roles": [ROLE_ID]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    if DRY_RUN or not WEBHOOK.startswith("https://"):
        print("[TEST - ikke sendt]", json.dumps(payload, ensure_ascii=False)[:1500])
        return
    for _ in range(3):
        try:
            r = session.post(WEBHOOK, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if r.status_code >= 300:
                print("Discord afviste:", r.status_code, r.text[:300])
            break
        except requests.RequestException as e:
            print("Discord-fejl:", e)
            break
    time.sleep(1.2)


# ---------- kampe ----------
def teams_of(title):
    t = (title or "").replace("Counter-Strike:", "").split(" - ")[0]
    t = re.sub(r"\(BO\d\)", "", t, flags=re.I)
    return [x.strip() for x in re.split(r"\s+vs\.?\s+", t, flags=re.I) if x.strip()]


def is_tier1(title):
    t = (title or "").lower() + " "
    return any(k in t for k in TIER1_KEYWORDS)


def scan_events():
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=HOURS_AFTER), now + timedelta(hours=HOURS_BEFORE)
    out, skipped = {}, 0
    for page in range(30):
        evs = get(f"{GAMMA}/events", {"tag_slug": "counter-strike-2", "closed": "false",
                                     "limit": 100, "offset": page * 100})
        if not evs:
            break
        for e in evs:
            title = e.get("title") or ""
            if not title.startswith("Counter-Strike:"):
                continue
            start = parse_time(e.get("startTime") or e.get("startDate"))
            if not start or not (lo <= start <= hi):
                continue
            vol = float(e.get("volume") or 0)
            if is_tier1(title) or vol > MAX_EVENT_VOLUME:
                skipped += 1
                continue
            teams = teams_of(title)
            out[str(e["id"])] = {
                "id": str(e["id"]), "title": title, "slug": e.get("slug", ""), "start": start,
                "teams": teams, "suspect": [SUSPECT[norm(t)] for t in teams if norm(t) in SUSPECT],
            }
        if len(evs) < 100:
            break
    print(f"{len(out)} lavere tier CS2-kampe i vinduet ({skipped} tier 1/store sprunget over)")
    return out


def fetch_trades(events):
    ids, rows = list(events), []
    for i in range(0, len(ids), 20):
        params = {"event_id": ",".join(ids[i:i + 20]), "side": "BUY", "limit": 1000,
                  "filter_type": "CASH", "filter_amount": MIN_TRADE_USD}
        for _ in range(MAX_PAGES):
            res = get(f"{DATA}/v2/trades", params)
            if not res:
                break
            rows += res.get("data") or []
            cur = (res.get("pagination") or {}).get("next_cursor")
            if not cur:
                break
            params = dict(params, cursor=cur)
    return rows


def wallet_info(w):
    hit = wallet_cache.get(w)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    res = get(f"{DATA}/v2/user-volume", {"user": w}) or {}
    d = res.get("data") or {}
    info = {"trades": d.get("trade_count"), "volume": d.get("volume_usdc")}
    wallet_cache[w] = (time.time(), info)
    return info


# ---------- analyse ----------
def analyse(events, rows):
    by_slug = {e["slug"]: e for e in events.values()}
    per = {}      # (wallet, event_id) -> samling
    total = {}    # wallet -> samlet på lavere tier
    for t in rows:
        w = (t.get("proxy_wallet") or "").lower()
        ev = by_slug.get(t.get("event_slug"))
        if not w or not ev:
            continue
        try:
            usd = float(t["size"]) * float(t["price"])
            price = float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        p = per.setdefault((w, ev["id"]), {"usd": 0.0, "n": 0, "trades": [], "name": None})
        p["usd"] += usd
        p["n"] += 1
        p["name"] = p["name"] or t.get("name") or t.get("pseudonym")
        p["trades"].append({"usd": usd, "price": price, "outcome": t.get("outcome"),
                            "market": t.get("title"), "ts": int(t.get("timestamp") or 0),
                            "tx": t.get("transaction_hash", "")})
        tw = total.setdefault(w, {"usd": 0.0, "n": 0, "events": set()})
        tw["usd"] += usd
        tw["n"] += 1
        tw["events"].add(ev["id"])
    return per, total


def step_of(value, steps):
    lvl = None
    for s in steps:
        if value >= s:
            lvl = s
    return lvl


def score_for(ev, p, info, pre_match, on_suspect):
    s, why = 0, []
    n = info.get("trades")
    if n is not None and n <= NEW_MAX_TRADES:
        s += 3; why.append("ny konto")
    elif n is not None and n <= 100:
        s += 1; why.append("få handler")
    vol = info.get("volume") or 0
    if vol and p["usd"] / vol >= 0.5:
        s += 2; why.append("stor del af kontoens omsætning")
    avg = sum(t["usd"] * t["price"] for t in p["trades"]) / p["usd"] if p["usd"] else 0
    if avg and avg < 0.35:
        s += 2; why.append("underdog")
    elif avg and avg < 0.5:
        s += 1; why.append("lav pris")
    if ev["suspect"]:
        s += 1; why.append("mistænkt hold i kampen")
    if on_suspect:
        s += 1; why.append("bet PÅ mistænkt hold")
    if pre_match:
        s += 1; why.append("før kampstart")
    if p["usd"] >= 2500:
        s += 2
    elif p["usd"] >= 1000:
        s += 1
    return s, why


def build_alert(w, ev, p, info, reasons, tot, now_ts):
    start_ts = int(ev["start"].timestamp())
    pre = sum(1 for t in p["trades"] if t["ts"] and t["ts"] < start_ts)
    sides = {}
    for t in p["trades"]:
        k = f"{t['outcome']} · {(t['market'] or '').replace('Counter-Strike: ', '')}"
        sides.setdefault(k, [0.0, 0.0, 0])
        sides[k][0] += t["usd"]
        sides[k][1] += t["usd"] * t["price"]
        sides[k][2] += 1
    suspect_norm = {norm(x) for x in ev["suspect"]}
    on_suspect = any(norm(t["outcome"]) in suspect_norm for t in p["trades"])
    score, why = score_for(ev, p, info, pre > 0, on_suspect)
    level = "🔴" if score >= 6 else "🟠" if score >= 3 else "🟡"
    color = 0xE01E1E if score >= 6 else 0xF39C12 if score >= 3 else 0xF1C40F

    name = p["name"] or short(w)
    lines = []
    for k, (usd, wp, n) in sorted(sides.items(), key=lambda x: -x[1][0])[:6]:
        avg = wp / usd if usd else 0
        mark = " ⚠️" if norm(k.split(" · ")[0]) in suspect_norm else ""
        lines.append(f"`${usd:>7,.0f}` på **{k.split(' · ')[0]}**{mark} til {avg*100:.0f}¢ ({n} bets)\n"
                     f"↳ {k.split(' · ', 1)[1][:80]}")
    biggest = max(t["usd"] for t in p["trades"])
    n_tr = info.get("trades")
    konto = (f"{n_tr} handler · ${info.get('volume') or 0:,.0f} omsat i alt"
             if n_tr is not None else "ukendt")
    if n_tr is not None and n_tr <= NEW_MAX_TRADES:
        konto = "🆕 " + konto
    status = "🔴 LIVE" if now_ts >= start_ts else "⏳ Før kampstart"
    match = ev["title"].replace("Counter-Strike: ", "")
    sus = f"\n⚠️ Mistænkt hold: **{', '.join(ev['suspect'])}**" if ev["suspect"] else ""
    known = "\n🔁 Allerede i Sussytracker" if w in KNOWN else ""

    fields = [
        {"name": "👤 Konto", "value": f"[{name}](https://polymarket.com/profile/{w})\n{konto}", "inline": False},
        {"name": "💰 Satset på kampen", "value": f"**${p['usd']:,.0f}** ({p['n']} bets)", "inline": True},
        {"name": "📏 Største bet", "value": f"${biggest:,.0f}", "inline": True},
        {"name": "🕒 Kamp", "value": f"{status} · <t:{start_ts}:R>", "inline": True},
        {"name": "🎯 Bets", "value": "\n".join(lines)[:1024] or "-", "inline": False},
    ]
    if tot and tot["usd"] > p["usd"] + 1:
        fields.append({"name": "📈 Lavere tier CS2 i alt (48 t)",
                       "value": f"${tot['usd']:,.0f} på {len(tot['events'])} kampe ({tot['n']} bets)",
                       "inline": False})
    fields.append({"name": f"{level} Score {score}", "value": ", ".join(why) or "-", "inline": False})
    fields.append({"name": "➕ Tilføj til Sussytracker (kopiér linjen)",
                   "value": f"`{w}  {p['name'] or 'fra-radar'}`", "inline": False})
    return {
        "username": "Konto-Radar",
        "content": f"{level} **{' + '.join(reasons)}** · {name} · ${p['usd']:,.0f}",
        "embeds": [{
            "author": {"name": f"🕵️ KONTO-RADAR · {' + '.join(reasons)}",
                       "url": f"https://polymarket.com/profile/{w}"},
            "title": match[:250],
            "url": f"https://polymarket.com/event/{ev['slug']}",
            "description": (sus + known).strip() or None,
            "color": color,
            "fields": fields,
            "footer": {"text": "Konto-Radar · 322 | Match Watch"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }, score


def run_round(events, state):
    rows = fetch_trades(events)
    per, total = analyse(events, rows)
    alerted = set(state["alerted"])
    now_ts = time.time()
    sent = 0
    cands = sorted(per.items(), key=lambda kv: -kv[1]["usd"])
    for (w, eid), p in cands:
        if sent >= MAX_ALERTS_PER_ROUND:
            break
        if p["usd"] < min(NEW_MIN_USD, BIG_BET_USD, LOOKUP_MIN_USD):
            continue
        ev = events[eid]
        reasons, keys = [], []
        for t in p["trades"]:
            if t["usd"] >= BIG_BET_USD:
                k = f"big|{t['tx']}|{w}"
                if k not in alerted:
                    keys.append(k)
                    if "💰 STORT BET" not in reasons:
                        reasons.append("💰 STORT BET")
        lvl = step_of(p["usd"], MANY_SMALL_STEPS)
        if lvl and p["n"] >= MANY_SMALL_COUNT:
            k = f"many|{w}|{eid}|{lvl}"
            if k not in alerted:
                keys.append(k)
                reasons.append(f"🧩 MANGE BETS (${lvl:,}+)")
        tot = total.get(w)
        tl = step_of(tot["usd"], TOTAL_STEPS) if tot else None
        if tl and tot["n"] >= TOTAL_COUNT and len(tot["events"]) >= 2:
            k = f"total|{w}|{tl}"
            if k not in alerted:
                keys.append(k)
                reasons.append(f"📈 SAMLET ${tl:,}+")
        info = wallet_info(w) if p["usd"] >= LOOKUP_MIN_USD else {}
        n_tr = info.get("trades")
        if n_tr is not None and n_tr <= NEW_MAX_TRADES and p["usd"] >= NEW_MIN_USD:
            k = f"new|{w}|{eid}"
            if k not in alerted:
                keys.append(k)
                reasons.append("🆕 NY KONTO")
        if not keys:
            continue
        if n_tr is not None and n_tr >= PRO_MIN_TRADES and p["usd"] < PRO_SKIP_BELOW_USD:
            alerted.update(keys)   # pro-konto med små beløb: marker som set, men meld ikke
            state["alerted"] += keys
            continue
        payload, score = build_alert(w, ev, p, info, reasons, tot, now_ts)
        post(payload, ping=score >= 6)
        alerted.update(keys)
        state["alerted"] += keys
        sent += 1
    print(time.strftime("%H:%M:%S"), f"{len(rows)} bets, {len(per)} konto/kamp-par, {sent} meldinger")


def update_status(events, state, tilstand="aktiv"):
    if not STATUS_HOOK.startswith("https://") or DRY_RUN:
        return
    now = int(time.time())
    sus = sum(1 for e in events.values() if e["suspect"])
    titel = {"aktiv": "🟢  KONTO-RADAR · ACTIVE", "stop": "🔴  KONTO-RADAR · STOPPET"}[tilstand]
    embed = {
        "title": titel,
        "description": (f"Overvåger **{len(events)}** lavere tier CS2-kampe "
                        f"({sus} med mistænkte hold).\n\n**Sidst tjekket:** <t:{now}:R>  (<t:{now}:T>)"),
        "color": 0x2ECC71 if tilstand == "aktiv" else 0xE01E1E,
        "footer": {"text": "Står 'sidst tjekket' til mere end 10 min siden, er botten stoppet"},
    }
    payload = {"username": "Konto-Radar · status", "embeds": [embed]}
    try:
        sid = state.get("status_id")
        if sid:
            r = session.patch(f"{STATUS_HOOK}/messages/{sid}", json=payload, timeout=10)
            if r.ok:
                return
        r = session.post(STATUS_HOOK + "?wait=true", json=payload, timeout=10)
        if r.ok:
            state["status_id"] = r.json().get("id")
    except requests.RequestException as e:
        print("status-fejl:", e)


def main():
    state = load_state()
    events = scan_events()
    last_scan = time.time()
    if state.get("version") != 1:
        post({"username": "Konto-Radar", "embeds": [{
            "title": "✅ Konto-Radar kører",
            "description": (
                "Jeg leder efter **nye og mistænkelige konti** på lavere tier CS2 (tier 1 springes over).\n\n"
                f"💰 **Stort bet** · én handel på ${BIG_BET_USD:,}+\n"
                f"🧩 **Mange bets** · ${MANY_SMALL_USD:,}+ på én kamp fordelt på {MANY_SMALL_COUNT}+ bets\n"
                f"📈 **Samlet** · ${TOTAL_USD:,}+ på lavere tier CS2 i alt\n"
                f"🆕 **Ny konto** · højst {NEW_MAX_TRADES} handler i alt og ${NEW_MIN_USD}+ på en kamp\n\n"
                "⚠️ = mistænkt hold · 🟡/🟠/🔴 = mistankescore\n"
                "Nederst i hver besked står linjen, I kan kopiere ind i Sussytracker."),
            "color": 0x2ECC71}]})
        state["version"] = 1
    t_end = time.time() + RUN_SECONDS
    while True:
        t0 = time.time()
        if t0 - last_scan >= RESCAN_EVERY:
            events = scan_events() or events
            last_scan = t0
        if events:
            run_round(events, state)
        update_status(events, state)
        save_state(state)
        if time.time() >= t_end:
            break
        time.sleep(max(0, SECONDS_BETWEEN - (time.time() - t0)))


if __name__ == "__main__":
    main()
