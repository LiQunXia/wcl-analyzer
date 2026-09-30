import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests

ROOT = Path(__file__).parent
CACHE = ROOT / "cache"
WCL = "https://www.warcraftlogs.com"
HOST, PORT = "127.0.0.1", 8000

BUCKET_SEC = 3
WINDOW_SEC = 15
ATTRIB_BEFORE_SEC, ATTRIB_AFTER_SEC = 3, 12
ANCHOR_BEFORE_SEC, ANCHOR_AFTER_SEC = 12, 6
BOSS_DEDUP_SEC = 4
MIN_BOSS_DAMAGE_SHARE = 0.015
IGNORED_DAMAGE = {1, 3}  # melee, falling
DIFFICULTY = {1: "随机", 3: "普通", 4: "英雄", 5: "史诗", 10: "大秘境"}


class WclError(Exception):
    pass


class WclClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome")
        self.lock = threading.Lock()

    def _pass_human_check(self, report_url):
        r = self.session.get(report_url, timeout=30)
        m = re.search(r'name="_token" value="([^"]+)"', r.text)
        if not m:
            return
        self.session.post(
            f"{WCL}/human-challenge",
            data={"_token": m.group(1)},
            headers={"Referer": report_url, "Origin": WCL},
            timeout=30,
        )

    def get_json(self, code, path):
        report_url = f"{WCL}/reports/{code}"
        with self.lock:
            for attempt in range(2):
                r = self.session.get(f"{WCL}{path}", headers={"Referer": report_url}, timeout=60)
                if "application/json" in r.headers.get("content-type", ""):
                    text = r.text
                    if text.startswith("Use the API"):
                        raise WclError("WCL 拒绝了请求（反爬提示），请稍后再试")
                    return json.loads(text)
                if r.status_code == 404:
                    raise WclError("找不到这份日志，可能是私有日志或链接有误")
                if attempt == 0:
                    self._pass_human_check(report_url)
            raise WclError(f"WCL 返回了非 JSON 内容（HTTP {r.status_code}），人机验证可能失败")


wcl = WclClient()


def cached(name, fetch, persist=True):
    path = CACHE / name
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    data = fetch()
    if persist:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


# ---------- spells (zhCN names + icons via Wowhead) ----------

_spell_lock = threading.Lock()
_spell_file = CACHE / "spells.json"
_spells = json.loads(_spell_file.read_text("utf-8")) if _spell_file.exists() else {}


def _fetch_spell(spell_id):
    try:
        r = requests.get(f"https://nether.wowhead.com/tooltip/spell/{spell_id}?locale=4", timeout=15)
        j = r.json()
        return spell_id, {"name": j.get("name"), "icon": j.get("icon")} if j.get("name") else None
    except Exception:
        return spell_id, None


def spell_info(spell_ids):
    missing = [i for i in {str(i) for i in spell_ids} if i not in _spells]
    if missing:
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(_fetch_spell, missing))
        with _spell_lock:
            for sid, info in results:
                if info:
                    _spells[sid] = info
            CACHE.mkdir(exist_ok=True)
            _spell_file.write_text(json.dumps(_spells, ensure_ascii=False, indent=0), "utf-8")
    return {int(i): _spells.get(str(i)) or {} for i in spell_ids}


# ---------- report ----------

def parse_link(link):
    m = re.search(r"reports/([A-Za-z0-9]{16})", link) or re.fullmatch(r"\s*([A-Za-z0-9]{16})\s*", link)
    if not m:
        raise WclError("无法识别的 WCL 链接，需要形如 https://www.warcraftlogs.com/reports/XXXX")
    fight = re.search(r"[?&]fight=(\d+)", link)
    return m.group(1), int(fight.group(1)) if fight else None


_meta_cache = {}
META_TTL_SEC = 300


def report_meta(code):
    hit = _meta_cache.get(code)
    if hit and time.time() - hit[0] < META_TTL_SEC:
        return hit[1]
    meta = wcl.get_json(code, f"/reports/fights-and-participants/{code}/0")
    _meta_cache[code] = (time.time(), meta)
    return meta


def fmt_fight(f):
    return {
        "id": f["id"],
        "name": f.get("name"),
        "difficulty": DIFFICULTY.get(f.get("difficulty"), str(f.get("difficulty"))),
        "kill": bool(f.get("kill")),
        "duration": round((f["end_time"] - f["start_time"]) / 1000),
        "bossPercentage": (f.get("fightPercentage") or 0) / 100,
    }


def api_report(link):
    code, fight_id = parse_link(link)
    meta = report_meta(code)
    fights = [fmt_fight(f) for f in meta["fights"] if f.get("boss")]
    return {"code": code, "fight": fight_id, "fights": fights}


def load_cooldowns():
    cds = json.loads((ROOT / "cooldowns.json").read_text("utf-8"))
    return {e["id"]: {"spec": spec, "category": e["category"]}
            for spec, entries in cds.items() if not spec.startswith("_") for e in entries}


def graph(code, fight_id, rng, view, hostile, persist):
    suffix = "_enemy" if hostile else ""
    return cached(f"{code}/{fight_id}_{view.split('-')[0]}{suffix}.json", lambda: wcl.get_json(
        code, f"/reports/graph/{view}/{rng}/ability/{int(hostile)}/0/0/0/0/0/-1.0.-1.-1/0/Any/Any/0/0"), persist)


def per_second(series, start, n_sec):
    arr = [0.0] * n_sec
    interval = series["pointInterval"] / 1000
    for i, rate in enumerate(series.get("data") or []):
        if rate:
            _spread(arr, (series["pointStart"] - start) / 1000 + i * interval, interval, rate)
    return arr


def event_list(series):
    for s in series:
        if "guid" not in s:
            continue
        for pair in s.get("events") or []:
            yield s, pair[0] if isinstance(pair, list) else pair


def boss_abilities(dmg, friendly_guids, cast_names, start, n_sec):
    """Damage-taken abilities that come from the encounter, merged by name (one spell often has several ids)."""
    series = [s for s in dmg.get("series", []) if "guid" in s]
    total = sum(s.get("total", 0) for s in series) or 1
    abilities = {}
    for s in series:
        g, name = s["guid"], s["name"]
        if g in IGNORED_DAMAGE or g in friendly_guids:
            continue
        if s.get("total", 0) < MIN_BOSS_DAMAGE_SHARE * total and name not in cast_names:
            continue
        sec = per_second(s, start, n_sec)
        a = abilities.get(name)
        if a is None:
            abilities[name] = {"spell": g, "total": s.get("total", 0), "sec": sec, "top": s.get("total", 0)}
            continue
        a["sec"] = [x + y for x, y in zip(a["sec"], sec)]
        a["total"] += s.get("total", 0)
        if s.get("total", 0) > a["top"]:
            a["spell"], a["top"] = g, s.get("total", 0)
    for a in abilities.values():
        a["avg"] = sum(a["sec"]) / max(n_sec, 1)
    return abilities


def attribute(t, abilities, n_sec, min_excess):
    """The boss ability whose damage rises most above its own baseline right around t."""
    i0, i1 = max(0, int(t) - ATTRIB_BEFORE_SEC), min(n_sec, int(t) + ATTRIB_AFTER_SEC)
    best, best_excess = None, min_excess
    for name, a in abilities.items():
        excess = sum(a["sec"][i0:i1]) - a["avg"] * (i1 - i0)
        if excess > best_excess:
            best, best_excess = name, excess
    return best


def api_fight(code, fight_id):
    meta = report_meta(code)
    fight = next((f for f in meta["fights"] if f["id"] == fight_id), None)
    if not fight:
        raise WclError(f"这份日志里没有第 {fight_id} 场战斗")
    start, end = fight["start_time"], fight["end_time"]
    persist = not fight.get("inProgress")
    rng = f"{code}/{fight_id}/{start}/{end}"
    dmg = graph(code, fight_id, rng, "damage-taken", False, persist)
    casts = graph(code, fight_id, rng, "casts", False, persist)
    enemy_casts = graph(code, fight_id, rng, "casts", True, persist)

    tag = f".{fight_id}."
    players = {p["id"]: p for p in meta["friendlies"] if tag in (p.get("fights") or "")}
    enemies = {e["id"]: e for e in meta["enemies"]}
    cooldowns = load_cooldowns()
    healer_specs = {c["spec"] for c in cooldowns.values()}
    specs = {pid: p.get("icon") for pid, p in players.items() if p.get("icon") in healer_specs}

    duration = (end - start) / 1000
    n_sec = int(duration) + 1
    total_series = next((s for s in dmg.get("series", []) if s.get("id") == "Total"), None)
    per_sec = per_second(total_series, start, n_sec) if total_series else [0.0] * n_sec
    n_buckets = (n_sec + BUCKET_SEC - 1) // BUCKET_SEC
    buckets = [sum(per_sec[b * BUCKET_SEC:(b + 1) * BUCKET_SEC]) / BUCKET_SEC for b in range(n_buckets)]
    avg_window = sum(per_sec) / max(duration, 1) * WINDOW_SEC

    raw_boss = []
    for s, ev in event_list(enemy_casts.get("series", [])):
        src = enemies.get(ev.get("sourceID"))
        if not src or ev.get("type") not in ("cast", "begincast"):
            continue
        raw_boss.append({
            "t": round((ev["timestamp"] - start) / 1000, 1),
            "spell": s["guid"],
            "en": s["name"],
            "icon": ev["ability"].get("abilityIcon", "").removesuffix(".jpg"),
            "isBoss": src.get("type") == "Boss",
        })
    raw_boss.sort(key=lambda c: c["t"])

    friendly_guids = {s["guid"] for s in casts.get("series", []) if "guid" in s}
    abilities = boss_abilities(dmg, friendly_guids, {c["en"] for c in raw_boss}, start, n_sec)

    boss_casts, last_seen = [], {}
    for c in raw_boss:
        if not (c["isBoss"] or c["en"] in abilities):
            continue
        if c["t"] - last_seen.get(c["en"], -99) < BOSS_DEDUP_SEC:
            continue
        last_seen[c["en"]] = c["t"]
        boss_casts.append(c)

    events = []
    for s, ev in event_list(casts.get("series", [])):
        if s["guid"] not in cooldowns:
            continue
        src = ev.get("sourceID")
        if src not in players or ev.get("type") != "cast":
            continue
        cd_spec = cooldowns[s["guid"]]["spec"]
        icon = players[src].get("icon") or ""
        if src not in specs and "-" not in icon and icon == cd_spec.split("-")[0]:
            specs[src] = cd_spec
        if specs.get(src) != cd_spec:
            continue
        ab = ev["ability"]
        t = round((ev["timestamp"] - start) / 1000, 1)
        i = int(t)
        window = sum(per_sec[i:i + WINDOW_SEC])
        boss = attribute(t, abilities, n_sec, 0.1 * avg_window)
        anchors = [b["t"] for b in raw_boss if b["en"] == boss
                   and t - ANCHOR_BEFORE_SEC <= b["t"] <= t + ANCHOR_AFTER_SEC]
        events.append({
            "t": t,
            "sourceID": src,
            "spell": ab["guid"],
            "en": ab["name"],
            "icon": ab.get("abilityIcon", "").removesuffix(".jpg"),
            "category": cooldowns[ab["guid"]]["category"],
            "ratio": round(window / avg_window, 2) if avg_window else None,
            "boss": boss,
            "anchor": min(anchors, key=lambda a: (a > t, abs(t - a))) if anchors else None,
        })
    events.sort(key=lambda e: e["t"])
    healers = [
        {"id": pid, "name": players[pid]["name"], "cls": players[pid].get("type"), "spec": spec}
        for pid, spec in specs.items()
    ]

    deaths = []
    for d in dmg.get("deaths", []):
        if d.get("type") != "death" or not d.get("targetIsFriendly") or d.get("targetID") not in players:
            continue
        if not (start <= d["timestamp"] <= end):
            continue
        ka = d.get("killingAbility") or {}
        deaths.append({
            "t": round((d["timestamp"] - start) / 1000, 1),
            "name": players[d["targetID"]]["name"],
            "cls": players[d["targetID"]].get("type"),
            "spell": ka.get("guid"),
            "en": ka.get("name"),
        })

    phase_names = next((p["phases"] for p in meta.get("phases", []) if p.get("boss") == fight.get("boss")), [])
    phases = [
        {"t": round((p["startTime"] - start) / 1000),
         "name": phase_names[p["id"] - 1] if 0 < p["id"] <= len(phase_names) else f"P{p['id']}"}
        for p in fight.get("phases") or []
    ]
    heroism = sorted({
        (round((h["startTime"] - start) / 1000), round((h["endTime"] - start) / 1000))
        for h in dmg.get("heroism", [])
    })
    heroism = [{"t": t, "end": e} for t, e in heroism]

    cast_icon = {c["en"]: c["icon"] for c in raw_boss}
    info = spell_info({e["spell"] for e in events} | {d["spell"] for d in deaths if d["spell"]}
                      | {a["spell"] for a in abilities.values()} | {c["spell"] for c in boss_casts})
    for item in events + deaths:
        item["zh"] = (info.get(item["spell"]) or {}).get("name")
    boss_info = {
        name: {
            "spell": a["spell"],
            "zh": info[a["spell"]].get("name"),
            "icon": cast_icon.get(name) or info[a["spell"]].get("icon"),
            "share": round(a["total"] / max(sum(x["total"] for x in abilities.values()), 1), 3),
        }
        for name, a in abilities.items()
    }
    for c in boss_casts:
        c["zh"] = info[c["spell"]].get("name") or boss_info.get(c["en"], {}).get("zh")
        del c["isBoss"]

    return {
        "code": code,
        "fight": fmt_fight(fight),
        "healers": healers,
        "bucketSec": BUCKET_SEC,
        "damage": [round(v) for v in buckets],
        "casts": events,
        "bossCasts": boss_casts,
        "bossAbilities": boss_info,
        "deaths": deaths,
        "phases": phases,
        "heroism": heroism,
    }


def _spread(arr, t0, length, rate):
    t1 = t0 + length
    sec = int(max(t0, 0))
    while sec < len(arr) and sec < t1:
        overlap = min(t1, sec + 1) - max(t0, sec)
        if overlap > 0:
            arr[sec] += rate * overlap
        sec += 1


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/report":
                return self._json(api_report(q.get("link", "")))
            if url.path == "/api/fight":
                return self._json(api_fight(q["code"], int(q["fight"])))
            self._send(404, b"not found", "text/plain")
        except WclError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"内部错误：{type(e).__name__}: {e}"}, 500)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    print(f"WCL 大招分析已启动：http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
