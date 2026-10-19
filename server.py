import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

import mdt_export
import wcl_verify
from localization import LANGUAGE, language, normalize_language, translate

def _resource_dir():
    """Directory holding the HTML/JSON assets.

    A PyInstaller one-file build unpacks them next to ``sys._MEIPASS``, so
    ``__file__`` points into a temporary directory that is deleted on exit.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def _data_dir():
    """Writable directory for the runtime cache (stays next to the sources in dev)."""
    env = os.environ.get("WCL_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home()) / "WCLAnalyzer"
    return Path(__file__).parent


ROOT = _resource_dir()
CACHE = _data_dir() / "cache"
WCL = "https://www.warcraftlogs.com"
HOST = os.environ.get("WCL_HOST", "127.0.0.1")
PORT = int(os.environ.get("WCL_PORT", "8000"))

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


def _wcl_cookie_path():
    return CACHE / "wcl_cookies.json"


def _verify_command(url):
    """启动验证窗口的命令。

    打包后重新运行 exe 自身（launcher 会在 ``--verify`` 分支里直接转交）；
    从源码运行时则是当前解释器 + ``wcl_verify.py``。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, wcl_verify.VERIFY_FLAG, url]
    return [sys.executable, str(ROOT / "wcl_verify.py"), url]


def _run_verify_window(url):
    """在子进程里弹出内嵌浏览器，等用户点完“我是人类”，取回 cookie。"""
    env = dict(os.environ)
    env["WCL_WEBVIEW_DIR"] = str(CACHE / "webview")
    env["WCL_LANGUAGE"] = language()
    kwargs = {}
    if os.name == "nt":
        # 子进程只需要 WebView 的窗口，不需要额外的控制台窗口。
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            _verify_command(url),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=wcl_verify.TIMEOUT_SEC + 60,
            env=env,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return {"error": translate("等待人工验证超时，请重试", "Human verification timed out. Please try again.")}
    except OSError as exc:
        return {"error": translate(f"无法启动验证窗口：{exc}", f"Could not start verification: {exc}")}

    result = wcl_verify.parse_result(proc.stdout)
    if result is None:
        detail = (proc.stderr or "").strip().splitlines()
        hint = detail[-1] if detail else translate(f"退出码 {proc.returncode}", f"exit code {proc.returncode}")
        return {"error": translate(f"验证窗口异常退出：{hint}", f"Verification window exited unexpectedly: {hint}")}
    return result


class WclClient:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome")
        self.session.headers["User-Agent"] = wcl_verify.USER_AGENT
        self.lock = threading.Lock()
        self.verify_lock = threading.Lock()
        self.verified_at = 0.0
        self._load_cookies()

    def _load_cookies(self):
        try:
            cookies = json.loads(_wcl_cookie_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._set_cookies(cookies)

    def _set_cookies(self, cookies):
        for c in cookies or []:
            try:
                self.session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain") or "",
                    path=c.get("path") or "/",
                    secure=bool(c.get("secure")),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def _save_cookies(self, cookies):
        try:
            path = _wcl_cookie_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies, ensure_ascii=False), "utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _request_json(self, path, report_url):
        # Internal WCL endpoints use lang=cn/en (locale/translate are ignored).
        # Keep pins and pagination filters intact and never mutate session headers.
        url = urlparse(path)
        query = parse_qs(url.query, keep_blank_values=True)
        query["lang"] = ["en" if language() == "en" else "cn"]
        path = url._replace(query=urlencode(query, doseq=True)).geturl()
        r = self.session.get(f"{WCL}{path}", headers={
            "Referer": report_url,
            "Accept-Language": "en-US,en;q=0.9" if language() == "en" else "zh-CN,zh;q=0.9",
        }, timeout=60)
        if "application/json" in r.headers.get("content-type", ""):
            text = r.text
            if text.startswith("Use the API"):
                raise WclError(translate("WCL 拒绝了请求（反爬提示），请稍后再试", "WCL rejected the request. Please try again later."))
            return json.loads(text)
        if r.status_code == 404:
            raise WclError(translate("找不到这份日志，可能是私有日志或链接有误", "Report not found. It may be private or the link may be incorrect."))
        if r.status_code == 429:
            raise WclError(translate("请求过于频繁，被 WCL 限流了，请稍后再试", "WCL is rate limiting requests. Please try again later."))
        if r.status_code >= 500:
            raise WclError(translate(f"WCL 服务暂时不可用（HTTP {r.status_code}），请稍后再试", f"WCL is temporarily unavailable (HTTP {r.status_code}). Please try again later."))
        if r.text.lstrip().startswith("Use the API"):
            raise WclError(translate("WCL 拒绝了请求（反爬提示），请稍后再试", "WCL rejected the request. Please try again later."))
        # 其余情况基本都是人机验证页，需要用户亲自点一下。
        return None

    def _verify(self, path, report_url):
        with self.verify_lock:
            # 别的线程可能刚验证完，先重试一次，避免重复弹窗。
            with self.lock:
                if self._request_json(path, report_url) is not None:
                    return
            if time.time() - self.verified_at < 60:
                raise WclError(translate("刚刚完成验证仍被 WCL 拦截，请稍后再试", "WCL still blocked the request after verification. Please try again later."))
            result = _run_verify_window(report_url)
            if "cookies" not in result:
                raise WclError(result.get("error") or translate("人机验证未完成", "Human verification was not completed."))
            self._save_cookies(result["cookies"])
            self._set_cookies(result["cookies"])
            self.verified_at = time.time()

    def get_json(self, code, path):
        report_url = f"{WCL}/reports/{code}"
        with self.lock:
            data = self._request_json(path, report_url)
        if data is not None:
            return data

        self._verify(path, report_url)

        with self.lock:
            data = self._request_json(path, report_url)
        if data is not None:
            return data
        raise WclError(translate("完成人机验证后 WCL 仍未返回数据，请稍后重试", "WCL returned no data after verification. Please try again later."))


wcl = WclClient()


def cached(name, fetch, persist=True):
    # Legacy caches contain the uploader's language; don't reuse them across locales.
    path = CACHE / "localized-v1" / language() / name
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    data = fetch()
    if persist:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


# ---------- localized spell names + icons via Wowhead ----------

_spell_lock = threading.Lock()
_spell_files = {lang: CACHE / f"spells_{lang}.json" for lang in ("zh-CN", "en")}


def _read_spell_cache(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


_spells = {lang: _read_spell_cache(path) for lang, path in _spell_files.items()}
if not _spells["zh-CN"]:
    _spells["zh-CN"] = _read_spell_cache(CACHE / "spells.json")


def _fetch_spell(spell_id, lang=None):
    locale = 0 if normalize_language(lang or language()) == "en" else 4
    try:
        r = requests.get(f"https://nether.wowhead.com/tooltip/spell/{spell_id}?locale={locale}", timeout=15)
        j = r.json()
        return spell_id, {"name": j.get("name"), "icon": j.get("icon")} if j.get("name") else None
    except Exception:
        return spell_id, None


def spell_info(spell_ids):
    lang = language()
    spell_ids = set(spell_ids)
    store = _spells[lang]
    missing = [i for i in {str(i) for i in spell_ids} if i not in store]
    if missing:
        with ThreadPoolExecutor(8) as pool:
            # ContextVar values aren't inherited by workers on all supported Pythons.
            results = list(pool.map(lambda sid: _fetch_spell(sid, lang), missing))
        with _spell_lock:
            for sid, info in results:
                if info:
                    store[sid] = info
            CACHE.mkdir(parents=True, exist_ok=True)
            _spell_files[lang].write_text(json.dumps(store, ensure_ascii=False, indent=0), "utf-8")
    return {int(i): store.get(str(i)) or {} for i in spell_ids}


# ---------- report ----------

def parse_link(link):
    m = re.search(r"reports/([A-Za-z0-9]{16})", link) or re.fullmatch(r"\s*([A-Za-z0-9]{16})\s*", link)
    if not m:
        raise WclError(translate("无法识别的 WCL 链接，需要形如 https://www.warcraftlogs.com/reports/XXXX", "Invalid WCL link. Use a link such as https://www.warcraftlogs.com/reports/XXXX"))
    fight = re.search(r"[?&]fight=(\d+)", link)
    return m.group(1), int(fight.group(1)) if fight else None


_meta_cache = {}
META_TTL_SEC = 300


def report_meta(code):
    key = (language(), code)
    hit = _meta_cache.get(key)
    if hit and time.time() - hit[0] < META_TTL_SEC:
        return hit[1]
    meta = wcl.get_json(code, f"/reports/fights-and-participants/{code}/0")
    _meta_cache[key] = (time.time(), meta)
    return meta


def fmt_fight(f):
    return {
        "id": f["id"],
        "name": f.get("name"),
        "difficulty": (DIFFICULTY if language() == "zh-CN" else
                       {1: "Raid Finder", 3: "Normal", 4: "Heroic", 5: "Mythic", 10: "Mythic+"}
                       ).get(f.get("difficulty"), str(f.get("difficulty"))),
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
        raise WclError(translate(f"这份日志里没有第 {fight_id} 场战斗", f"Fight {fight_id} was not found in this report."))
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


# ---------- mythic+ route ----------

PULL_PRE_SEC, PULL_POST_SEC = 12, 3
MIN_PULL_SEC = 5
TANK_SPECS = {"Blood", "Protection", "Guardian", "Brewmaster", "Vengeance"}
HEALER_SPECS = {"Restoration", "Holy", "Discipline", "Mistweaver", "Preservation"}
AFFIX = {9: "残暴", 10: "强韧", 147: "萨拉塔斯的狡诈", 148: "晋升：萨拉塔斯之力", 152: "挑战者的危境",
         158: "虚空缚誓", 159: "萨拉塔斯的契约：宿主", 160: "萨拉塔斯的契约：吞噬",
         161: "萨拉塔斯的契约：脉冲", 162: "萨拉塔斯的契约：交织"}
AFFIX_EN = {9: "Tyrannical", 10: "Fortified", 147: "Xal'atath's Guile", 148: "Xal'atath's Bargain: Ascendant",
            152: "Challenger's Peril", 158: "Xal'atath's Bargain: Voidbound", 159: "Xal'atath's Bargain: Oblivion",
            160: "Xal'atath's Bargain: Devour", 162: "Xal'atath's Bargain: Pulsar"}
MAP_URL = "https://assets.rpglogs.com/img/warcraft/maps/{}-map.png"


def load_mplus_spells():
    data = json.loads((ROOT / "mplus_spells.json").read_text("utf-8"))
    by_class = {cls: {sid: cat for cat in ("offensive", "defensive") for sid in v.get(cat, [])}
                for cls, v in data.items() if not cls.startswith("_")}
    g = data["_global"]
    common = {sid: cat for cat in ("interrupt", "rez", "lust", "potion", "healPotion") for sid in g.get(cat, [])}
    common.update({sid: "offensive" for sid in g.get("racialOffensive", [])})
    common.update({sid: "defensive" for sid in g.get("racialDefensive", [])})
    return by_class, common


def classify_cast(guid, name, cls, by_class, common):
    cat = common.get(guid) or by_class.get(cls, {}).get(guid)
    if cat:
        return cat
    if re.search(r"药水|饮剂|Potion|Elixir", name or ""):
        return "healPotion" if re.search(r"生命|治疗|Healing|Health", name) else (
            None if re.search(r"法力|Mana", name) else "potion")
    return None


def _mplus_aura_events(code, fight_id, start, end, aura_type, spell_ids=(), spell_names=(),
                       target_ids=(), persist=True):
    """Fetch complete aura event pages, optionally limited to tracked spells."""
    ids = sorted({int(sid) for sid in spell_ids})
    targets = sorted({int(pid) for pid in target_ids})
    # These characters delimit WCL's pin format even inside quoted expressions.
    names = sorted({name for name in spell_names if isinstance(name, str) and name
                    and not any(char in name for char in "$^\r\n")})
    if not ids and not names and aura_type != "debuffs":
        return []
    clauses = []
    if ids:
        clauses.append(f"ability.id IN ({','.join(map(str, ids))})")
    if names:
        clauses.append("ability.name IN (" + ",".join(json.dumps(name, ensure_ascii=False) for name in names) + ")")
    if targets:
        clauses.append(f"target.id IN ({','.join(map(str, targets))})")
    query = ""
    if clauses:
        pin = "2$Off$#909049$expression$" + " OR ".join(clauses)
        query = "?" + urlencode({"pins": pin})
    fingerprint = hashlib.sha256(json.dumps([aura_type, start, end, ids, names, targets], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def fetch():
        events = []
        cursor = start
        while cursor <= end:
            path = (f"/reports/auras-events/{code}/{fight_id}/{cursor}/{end}"
                    f"/{aura_type}/0/0/0/0/0/source/0/-1.0.-1.-1/0/Any/Any/0{query}")
            page = wcl.get_json(code, path)
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise WclError(translate("WCL 未返回完整的光环事件数据，请稍后重试", "WCL returned incomplete aura events. Please try again later."))
            events.extend(page["events"])
            next_cursor = page.get("nextPageTimestamp")
            if next_cursor is None:
                return events
            if not isinstance(next_cursor, int) or isinstance(next_cursor, bool) or next_cursor <= cursor:
                raise WclError(translate("WCL 光环事件分页未能继续，请稍后重试", "WCL aura event pagination did not advance. Please try again later."))
            cursor = next_cursor
        return events

    if aura_type == "buffs":
        legacy_fingerprint = hashlib.sha256(json.dumps([start, end, ids, names], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        cache_name = f"{code}/{fight_id}_buff_events_v1_{legacy_fingerprint}.json"
    else:
        cache_name = f"{code}/{fight_id}_{aura_type}_events_v1_{fingerprint}.json"
    return cached(cache_name, fetch, persist)


def mplus_buff_events(code, fight_id, start, end, spell_ids, spell_names=(), persist=True):
    """Fetch complete buff event pages, limited to the timeline's tracked spells."""
    return _mplus_aura_events(code, fight_id, start, end, "buffs", spell_ids, spell_names, persist=persist)


def mplus_debuff_events(code, fight_id, start, end, target_ids, persist=True):
    """Fetch debuff events so defensive casts can be matched to nearby removals."""
    return _mplus_aura_events(code, fight_id, start, end, "debuffs", target_ids=target_ids, persist=persist)


def source_graph(code, fight_id, rng, view, persist, hostile=False):
    """Graph grouped by source (player) rather than ability."""
    suffix = "_enemy" if hostile else ""
    return cached(f"{code}/{fight_id}_src_{view}{suffix}.json", lambda: wcl.get_json(
        code, f"/reports/graph/{view}/{rng}/source/{int(hostile)}/0/0/0/0/0/-1.0.-1.-1/0/Any/Any/0/0"), persist)


def player_role(icon):
    spec = (icon or "").split("-")[-1]
    return "tank" if spec in TANK_SPECS else "healer" if spec in HEALER_SPECS else "dps"


def known_non_tank_role(icon):
    """Only infer defensive purpose when WCL identified the player's specialization."""
    return bool(icon and "-" in icon and player_role(icon) != "tank")


DEFENSIVE_CONTEXT_BEFORE_SEC = 1.0
DEFENSIVE_CONTEXT_AFTER_SEC = 6.0
DEFENSIVE_CAST_CONTEXT_SEC = 2.0
DEBUFF_REMOVAL_WINDOW_SEC = 0.75


def mplus_damage_events(series, players, start):
    """Extract player-targeted damage and immune misses from the damage-taken graph."""
    events = []
    for s, ev in event_list(series):
        target_id = ev.get("targetID") or (ev.get("target") or {}).get("id")
        if target_id not in players or ev.get("type") not in {"damage", "miss"}:
            continue
        ability = ev.get("ability") or {}
        name = s.get("name") or ability.get("name")
        if not name or not isinstance(ev.get("timestamp"), (int, float)):
            continue
        miss_type = str(ev.get("missType") or "").casefold()
        is_immune = ev.get("type") == "miss" and "immune" in miss_type
        is_dot = bool(ev.get("tick") or ev.get("isTick") or ev.get("periodic"))
        events.append({
            "t": (ev["timestamp"] - start) / 1000,
            "who": target_id,
            "spell": s.get("guid") or ability.get("guid"),
            "name": name,
            "kind": "immune" if is_immune else "dot" if is_dot else "damage",
        })
    return events


def mplus_hostile_cast_events(series, players, start):
    """Extract nearby enemy casts, including casts that an immunity may prevent from landing."""
    events = []
    for s, ev in event_list(series):
        if ev.get("type") not in {"cast", "begincast"}:
            continue
        if not isinstance(ev.get("timestamp"), (int, float)):
            continue
        target_id = ev.get("targetID") or (ev.get("target") or {}).get("id")
        if target_id in players:
            who = target_id
        elif target_id is None:
            who = None
        else:
            continue
        name = s.get("name") or (ev.get("ability") or {}).get("name")
        if name:
            events.append({"t": (ev["timestamp"] - start) / 1000, "who": who, "name": name})
    return events


def defensive_purpose(target_id, cast_time, damage_events, debuff_events, hostile_casts, start):
    """Return the closest log evidence around a defensive cast, without asserting causality."""
    removals = []
    for ev in debuff_events:
        event_target = ev.get("targetID") or (ev.get("target") or {}).get("id")
        if ev.get("type") != "removedebuff" or event_target != target_id:
            continue
        ability = ev.get("ability") or {}
        name = ability.get("name")
        timestamp = ev.get("timestamp")
        if name and isinstance(timestamp, (int, float)):
            t = (timestamp - start) / 1000
            delta = t - cast_time
            if abs(delta) <= DEBUFF_REMOVAL_WINDOW_SEC:
                removals.append((abs(delta), t, {"kind": "debuff", "name": name, "delta": delta}))
    if removals:
        return min(removals, key=lambda x: x[0])[2]

    candidates = [ev for ev in damage_events if ev["who"] == target_id
                  and -DEFENSIVE_CONTEXT_BEFORE_SEC <= ev["t"] - cast_time <= DEFENSIVE_CONTEXT_AFTER_SEC]
    immune = [ev for ev in candidates if ev["kind"] == "immune"]
    if immune:
        ev = min(immune, key=lambda e: abs(e["t"] - cast_time))
        return {"kind": "immune", "name": ev["name"], "delta": ev["t"] - cast_time}

    after = [ev for ev in candidates if ev["t"] >= cast_time]
    if after:
        ev = min(after, key=lambda e: e["t"])
    elif candidates:
        ev = max(candidates, key=lambda e: e["t"])
    else:
        casts = [ev for ev in hostile_casts if ev["who"] in (None, target_id)
                 and abs(ev["t"] - cast_time) <= DEFENSIVE_CAST_CONTEXT_SEC]
        if casts:
            ev = min(casts, key=lambda e: abs(e["t"] - cast_time))
            return {"kind": "enemy_cast", "name": ev["name"], "delta": ev["t"] - cast_time}
        return {"kind": "unknown"}
    return {"kind": ev["kind"], "name": ev["name"], "delta": ev["t"] - cast_time}


def annotate_defensive_purposes(casts, buffs, damage_events, debuff_events, hostile_casts, players, start):
    """Attach nearby evidence to non-tank defensive casts and their matching aura rows."""
    defensive_buffs = [b for b in buffs if b.get("cat") == "defensive"]
    for cast in casts:
        caster = cast.get("who")
        if cast.get("cat") != "defensive" or caster not in players or not known_non_tank_role(players[caster].get("icon")):
            continue
        linked = [b for b in defensive_buffs if b.get("source") == caster
                  and (b.get("spell") == cast.get("spell") or b.get("name") == cast.get("name"))
                  and abs(b.get("t", cast["t"]) - cast["t"]) <= 1]
        targets = sorted({b["who"] for b in linked if b.get("who") in players}) or [caster]
        purposes = []
        for target in targets:
            purpose = defensive_purpose(target, cast["t"], damage_events, debuff_events, hostile_casts, start)
            if target != caster:
                purpose["target"] = players[target].get("name")
            purposes.append(purpose)
            for buff in linked:
                if buff.get("who") == target:
                    buff["purpose"] = purpose
        cast["purposes"] = purposes


def map_image(map_id):
    path = CACHE / "maps" / f"{map_id}.png"
    if not path.exists():
        r = requests.get(MAP_URL.format(map_id), timeout=60)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise WclError(translate(f"地图 {map_id} 获取失败（HTTP {r.status_code}）", f"Could not load map {map_id} (HTTP {r.status_code})."))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return path.read_bytes()


def real_pull(p):
    """WCL sometimes records a few-second "pull" with no enemies (e.g. a floor transition)."""
    return bool(p.get("enemies")) or (p["end_time"] - p["start_time"]) / 1000 >= MIN_PULL_SEC


def mplus_buff_intervals(events, casts, players, start, end, pet_owners=None):
    """Pair actual aura events per recipient/caster; refreshes extend the same interval."""
    by_class, common = load_mplus_spells()
    cast_names = {(c["who"], c["name"]): c["cat"] for c in casts}
    active, intervals, seen = {}, [], set()
    pet_owners = pet_owners or {}

    def source_id(ev):
        return ev.get("sourceID", (ev.get("source") or {}).get("id"))

    def source_owner(ev):
        source = source_id(ev)
        return pet_owners.get(source, source)

    def category(ev):
        ability = ev.get("ability") or {}
        owner = source_owner(ev)
        if owner is not None and owner not in players:
            return None
        owner = owner if owner in players else ev.get("targetID")
        cat = classify_cast(ability.get("guid"), ability.get("name"),
                            players.get(owner, {}).get("type"), by_class, common)
        return cat or cast_names.get((owner, ability.get("name")))

    def open_interval(ev, known, initial=False):
        ability = ev["ability"]
        return {
            "t": start if initial else max(start, ev["timestamp"]),
            "who": ev["targetID"], "source": source_owner(ev),
            "spell": ability["guid"], "name": ability.get("name") or str(ability["guid"]),
            "icon": (ability.get("abilityIcon") or "").removesuffix(".jpg"),
            "cat": category(ev), "startKnown": known and ev["timestamp"] >= start,
        }

    def close_interval(item, timestamp, known):
        stop = min(timestamp, end)
        if stop <= item["t"]:
            return
        intervals.append({**item, "t": round((item["t"] - start) / 1000, 3),
                          "end": round((stop - start) / 1000, 3),
                          "endKnown": known and timestamp <= end})

    for ev in sorted(events, key=lambda e: e["timestamp"]):
        kind = ev.get("type")
        if (kind not in {"applybuff", "refreshbuff", "applybuffstack", "removebuffstack", "removebuff"}
                or ev.get("targetID") not in players or ev.get("sourceIsFriendly") is False
                or not (ev.get("ability") or {}).get("guid")):
            continue
        key = (ev["targetID"], ev["ability"]["guid"], source_id(ev))
        raw_key = key
        # Some removal events omit the source. Only fall back when unambiguous.
        if key not in active:
            matches = [k for k in active if k[:2] == key[:2]]
            if source_id(ev) is None:
                if len(matches) == 1:
                    key = matches[0]
                elif len(matches) > 1:
                    continue
            elif len(matches) == 1 and matches[0][2] is None and source_owner(ev) in players:
                # An application may omit the caster while a later event identifies it.
                item = active.pop(matches[0])
                item["source"] = source_owner(ev)
                active[key] = item
        if kind == "removebuff":
            item = active.pop(key, None)
            if item is None and key not in seen and category(ev):
                item = open_interval(ev, False, initial=True)
            if item:
                close_interval(item, ev["timestamp"], True)
        elif key not in active and category(ev):
            # A first refresh/stack event means the aura was already active at the run boundary.
            active[key] = open_interval(ev, kind == "applybuff", initial=kind != "applybuff" and key not in seen)
        seen.add(key)
        seen.add(raw_key)

    for item in active.values():
        close_interval(item, end, False)
    return sorted(intervals, key=lambda b: (b["t"], b["who"], b["spell"]))


def api_mplus_list(link):
    code, fight_id = parse_link(link)
    meta = report_meta(code)
    runs = [{
        "id": f["id"],
        "name": f.get("zoneName") or f.get("name"),
        "level": f.get("keystoneLevel"),
        "kill": bool(f.get("kill")),
        "duration": round((f["end_time"] - f["start_time"]) / 1000),
        "pulls": sum(map(real_pull, f["dungeonPulls"])),
    } for f in meta["fights"] if f.get("dungeonPulls")]
    if not runs:
        raise WclError(translate("这份日志里没有找到大秘境战斗", "No Mythic+ runs were found in this report."))
    return {"code": code, "fight": fight_id, "runs": runs}


def api_mplus(code, fight_id):
    meta = report_meta(code)
    fight = next((f for f in meta["fights"] if f["id"] == fight_id), None)
    if not fight or not fight.get("dungeonPulls"):
        raise WclError(translate(f"第 {fight_id} 场不是大秘境战斗（没有拉怪数据）", f"Fight {fight_id} is not a Mythic+ run (no pull data)."))
    start, end = fight["start_time"], fight["end_time"]
    n_sec = int((end - start) / 1000) + 1
    persist = not fight.get("inProgress")
    rng = f"{code}/{fight_id}/{start}/{end}"
    rel = lambda ts: round((ts - start) / 1000, 1)

    tag = f".{fight_id}."
    players = {p["id"]: p for p in meta["friendlies"]
               if tag in (p.get("fights") or "") and p.get("type") in CLASS_TYPES}
    enemies = {e["id"]: e for e in meta["enemies"]}
    count_map = {int(k): v for k, v in (fight.get("npcCountMap") or {}).items()}
    required = fight.get("countRequired") or 1

    casts = graph(code, fight_id, rng, "casts", False, persist)
    taken = graph(code, fight_id, rng, "damage-taken", False, persist)
    enemy_taken = graph(code, fight_id, rng, "damage-taken", True, persist)
    done_src = source_graph(code, fight_id, rng, "damage-done", persist)
    heal_src = source_graph(code, fight_id, rng, "healing", persist)
    taken_src = source_graph(code, fight_id, rng, "damage-taken", persist)

    # pulls
    pulls = []
    for p in fight["dungeonPulls"]:
        if not real_pull(p):
            continue
        engaged = {}
        for g, actor_id, lo, hi, *_ in p.get("enemies") or []:
            e = enemies.get(actor_id) or {}
            engaged.setdefault(g, {"guid": g, "name": e.get("name") or str(g), "engaged": 0, "killed": 0,
                                   "count": count_map.get(g, 0), "boss": e.get("type") == "Boss"})
            engaged[g]["engaged"] += hi - lo + 1
        pulls.append({
            "id": p["id"], "name": p.get("name"), "boss": p.get("boss") or 0,
            "start": rel(p["start_time"]), "end": rel(p["end_time"]),
            "_ts": (p["start_time"], p["end_time"]),
            "x": p.get("x"), "y": p.get("y"),
            "box": [p.get("minX"), p.get("minY"), p.get("maxX"), p.get("maxY")],
            "map": (p.get("maps") or [0])[0], "maps": p.get("maps") or [],
            "enemies": engaged, "forces": 0, "casts": [], "deaths": [], "players": {},
        })

    def pull_at(ts, pre=PULL_PRE_SEC * 1000, post=PULL_POST_SEC * 1000):
        best = None
        for p in pulls:
            s, e = p["_ts"]
            if s - pre <= ts <= e + post and (best is None or abs(ts - s) < abs(ts - best["_ts"][0])):
                best = p
        return best

    # enemy kills -> forces
    for d in enemy_taken.get("deaths", []):
        if d.get("targetIsFriendly"):
            continue
        actor = enemies.get(d.get("targetID")) or {}
        g = actor.get("guid") or (d.get("target") or {}).get("guid")
        name = actor.get("name") or (d.get("target") or {}).get("name")
        p = pull_at(d["timestamp"], 1000, 1000)
        if not p or g is None:
            continue
        e = p["enemies"].setdefault(g, {"guid": g, "name": name, "engaged": 0, "killed": 0,
                                        "count": count_map.get(g, 0), "boss": actor.get("type") == "Boss"})
        e["killed"] += 1
        e["engaged"] = max(e["engaged"], e["killed"])
        p["forces"] += count_map.get(g, 0)

    # player casts
    by_class, common = load_mplus_spells()
    usage = {pid: {} for pid in players}
    lust, tracked_casts = [], []
    for s, ev in event_list(casts.get("series", [])):
        src = ev.get("sourceID")
        if ev.get("type") != "cast" or src not in players:
            continue
        cat = classify_cast(s["guid"], s["name"], players[src].get("type"), by_class, common)
        if not cat:
            continue
        ab = ev.get("ability") or {}
        item = {"t": rel(ev["timestamp"]), "who": src, "spell": s["guid"], "name": s["name"],
                "icon": (ab.get("abilityIcon") or "").removesuffix(".jpg"), "cat": cat}
        tracked_casts.append(item)
        if cat == "lust":
            lust.append(item)
        u = usage[src].setdefault(s["guid"], {"spell": s["guid"], "name": item["name"], "icon": item["icon"],
                                              "cat": cat, "times": []})
        u["times"].append(item["t"])
        p = pull_at(ev["timestamp"])
        if p:
            p["casts"].append(item)
    for p in pulls:
        p["casts"].sort(key=lambda c: c["t"])

    # friendly deaths
    deaths = []
    for d in taken.get("deaths", []):
        if d.get("type") != "death" or d.get("targetID") not in players or d.get("feign"):
            continue
        ka = d.get("killingAbility") or {}
        item = {"t": rel(d["timestamp"]), "who": d["targetID"], "spell": ka.get("guid"), "name": ka.get("name"),
                "icon": (ka.get("abilityIcon") or "").removesuffix(".jpg")}
        deaths.append(item)
        p = pull_at(d["timestamp"], 0, 8000)
        if p:
            p["deaths"].append(item)
    deaths.sort(key=lambda d: d["t"])

    buff_ids = set(common)
    for player in players.values():
        buff_ids.update(by_class.get(player.get("type"), {}))
    buff_events = mplus_buff_events(code, fight_id, start, end, buff_ids,
                                   [c["name"] for c in tracked_casts], persist=persist)
    pet_owners = {pet["id"]: pet.get("petOwner") for pet in meta.get("friendlyPets", [])}
    buffs = mplus_buff_intervals(buff_events, tracked_casts, players, start, end, pet_owners)
    non_tank_defensives = [c for c in tracked_casts if c["cat"] == "defensive"
                           and known_non_tank_role(players[c["who"]].get("icon"))]
    debuff_events = (mplus_debuff_events(code, fight_id, start, end, players, persist=persist)
                     if non_tank_defensives else [])
    damage_events = mplus_damage_events(taken.get("series", []), players, start)
    hostile_casts = (mplus_hostile_cast_events(graph(code, fight_id, rng, "casts", True, persist).get("series", []),
                                               players, start) if non_tank_defensives else [])
    annotate_defensive_purposes(tracked_casts, buffs, damage_events, debuff_events,
                                hostile_casts, players, start)

    # per-pull player throughput
    def per_player(g):
        return {s["id"]: per_second(s, start, n_sec) for s in g.get("series", []) if s.get("id") in players}
    metrics = {"dmg": per_player(done_src), "heal": per_player(heal_src), "taken": per_player(taken_src)}
    for p in pulls:
        i0, i1 = int(p["start"]), int(p["end"]) + 1
        p["players"] = {pid: {k: round(sum(m[pid][i0:i1])) if pid in m else 0 for k, m in metrics.items()}
                        for pid in players}

    cumulative = 0
    for p in pulls:
        cumulative += p["forces"]
        p["forcesPct"] = round(p["forces"] / required * 100, 2)
        p["cumPct"] = round(cumulative / required * 100, 2)
        p["enemies"] = sorted(p["enemies"].values(), key=lambda e: (-e["boss"], -e["count"] * e["killed"], e["name"]))
        del p["_ts"]

    maps, seen = [], set()
    for m in (fight.get("dungeonReplay") or {}).get("maps", []):
        if m["mapID"] not in seen:
            seen.add(m["mapID"])
            maps.append({"id": m["mapID"], "name": m.get("name"), "minX": m["mapMinX"], "maxX": m["mapMaxX"],
                         "minY": m["mapMinY"], "maxY": m["mapMaxY"]})

    roster = sorted(({
        "id": pid, "name": p["name"], "cls": p.get("type"), "spec": (p.get("icon") or "").split("-")[-1],
        "role": player_role(p.get("icon")), "server": p.get("server"),
        "total": {k: round(sum(m.get(pid, []))) for k, m in metrics.items()},
        "usage": sorted(usage[pid].values(), key=lambda u: (u["cat"], u["times"][0])),
    } for pid, p in players.items()), key=lambda r: (["tank", "healer", "dps"].index(r["role"]), r["name"]))

    return {
        "code": code,
        "run": {
            "id": fight_id, "name": fight.get("zoneName") or fight.get("name"),
            "level": fight.get("keystoneLevel"), "kill": bool(fight.get("kill")),
            "duration": rel(end), "completion": round((fight.get("completionTime") or 0) / 1000),
            "medal": fight.get("medal"), "rating": fight.get("rating"),
            "affixes": [(AFFIX if language() == "zh-CN" else AFFIX_EN).get(a, translate(f"词缀 {a}", f"Affix {a}"))
                        for a in fight.get("affixes") or []],
            "countRequired": required, "countReached": fight.get("countReached"),
        },
        "maps": maps,
        "players": roster,
        "pulls": pulls,
        "deaths": deaths,
        "lust": lust,
        "buffs": buffs,
        "heroism": [{"t": rel(h["startTime"]), "end": rel(h["endTime"])} for h in taken.get("heroism", [])],
    }


REPLAY_SEGMENT_MS = 240000
PATH_STEP_SEC = 2


def api_mplus_paths(code, fight_id):
    """Player tracks and enemy spawn spots, extracted from WCL's combat-replay segments (large, so cached)."""
    meta = report_meta(code)
    fight = next((f for f in meta["fights"] if f["id"] == fight_id), None)
    if not fight or not fight.get("dungeonPulls"):
        raise WclError(translate(f"第 {fight_id} 场不是大秘境战斗（没有拉怪数据）", f"Fight {fight_id} is not a Mythic+ run (no pull data)."))
    persist = not fight.get("inProgress")
    data = cached(f"{code}/{fight_id}_paths_v2.json", lambda: _extract_paths(code, fight, meta), persist)
    # Enemy names/icons come from the (cached) report metadata, so old path caches stay valid.
    guids = {m.get("guid") for m in data.get("mobs") or []}
    counts = {int(k): v for k, v in (fight.get("npcCountMap") or {}).items()}
    data["enemies"] = {
        e["guid"]: {"name": e.get("name") or "", "icon": e.get("icon") or "",
                    "type": e.get("type") or "", "count": counts.get(e["guid"], 0)}
        for e in meta["enemies"] if e.get("guid") in guids
    }
    data["tankRoute"] = mplus_tank_route(fight, meta, data)
    return data


def mplus_tank_route(fight, meta, paths):
    """Conservative action sketch using replay interactions, deaths and tank samples.

    A batch is a time window of first *recorded interactions*, not an MDT spawn pack.
    Never infer aggro, range, a kill, or enemy convergence from a missing event.
    Coordinates on joins/stops are the tank's observed position, not enemy spawns.
    """
    players = {p["id"]: p for p in meta["friendlies"]
               if f".{fight['id']}." in (p.get("fights") or "") and p.get("type") in CLASS_TYPES}
    tank = next((pid for pid, p in players.items() if player_role(p.get("icon")) == "tank"), None)
    pulls = [p for p in fight.get("dungeonPulls", []) if real_pull(p)]
    result = {"tank": tank, "pulls": []}
    if tank is None:
        return result
    start = fight["start_time"]
    relative = lambda ts: round((ts - start) / 1000, 1)
    track = sorted(paths.get("tracks", {}).get(str(tank), paths.get("tracks", {}).get(tank, [])))
    maps = (fight.get("dungeonReplay") or {}).get("maps", [])

    def locate(t, pull):
        # Use an actual nearby sample; do not bridge missing movement or floors.
        near = min(track, key=lambda q: abs(q[0] - t), default=None)
        if near is None or abs(near[0] - t) > 3:
            return {}
        if len(near) > 3 and near[3] is not None:
            if near[3] not in (pull.get("maps") or []):
                return {}
            return {"x": near[1], "y": near[2], "map": near[3]}
        floors = [m for m in maps if m["mapID"] in (pull.get("maps") or [])
                  and m["mapMinX"] <= near[1] <= m["mapMaxX"]
                  and m["mapMinY"] <= near[2] <= m["mapMaxY"]]
        if len(floors) != 1:
            return {}
        return {"x": near[1], "y": near[2], "map": floors[0]["mapID"]}

    died = {(d["actor"], d["instance"]): d["t"] for d in paths.get("enemyDeaths", [])}
    contacts = sorted(paths.get("contacts", []), key=lambda c: c["t"])
    assigned = set()
    prior = []
    for pull in pulls:
        lo, hi = relative(pull["start_time"]), relative(pull["end_time"])
        if pull.get("boss"):
            result["pulls"].append({"id": pull["id"], "start": lo, "end": hi, "steps": [],
                                    "segments": [], "observed": 0, "boss": True})
            continue
        members = {(actor, instance) for _, actor, lower, upper, *_ in pull.get("enemies", [])
                   for instance in range(lower, upper + 1)}
        own = [c for c in contacts if (c["actor"], c["instance"]) in members
               and (fight.get("npcCountMap") or {}).get(str(c["guid"]), 0) > 0
               and (c["actor"], c["instance"]) not in assigned and lo - 2 <= c["t"] <= hi]
        batches = []
        for c in own:
            key = (c["actor"], c["instance"])
            assigned.add(key)
            pos = locate(c["t"], pull)
            batch = batches[-1] if batches else None
            if (batch is None or c["t"] - batch["t"] > 5
                    or (pos and "x" in batch and math.hypot(pos["x"] - batch["x"], pos["y"] - batch["y"]) > 2500)):
                batch = {"kind": "join", "t": c["t"], "until": c["t"], "label": "",
                         "members": [], **pos}
                batches.append(batch)
            batch["members"].append(c)
            batch["until"] = c["t"]

        for i, batch in enumerate(batches):
            batch["label"] = chr(65 + i) if i < 26 else str(i + 1)
            older = [c for c in prior + own if c["t"] < batch["t"] - 5]
            # A future recorded death proves survival; last coordinate does not.
            alive = [c for c in older if died.get((c["actor"], c["instance"]), -1) > batch["t"]
                     and (c["actor"], c["instance"]) in members]
            batch["alive"] = len(alive)
            batch["chain"] = bool(alive and batch["t"] - min(c["t"] for c in alive) >= 15 and not pull.get("boss"))
            recent = [c for c in older if batch["t"] - 8 <= died.get((c["actor"], c["instance"]), -100) < batch["t"]]
            batch["recentDeaths"] = [{"name": c["name"], "t": died[(c["actor"], c["instance"])]} for c in recent]
            tank_hits = [c for c in batch["members"] if c["who"] == tank and c.get("outgoing")]
            batch["tankAction"] = tank_hits[0] if tank_hits else None

        # Find sustained small-radius tank stays. These do not prove enemies gathered.
        samples = [q for q in track if lo <= q[0] <= hi]
        stops, window = [], []
        for q in samples + [[hi + 10, 0, 0]]:
            changed_floor = (window and len(q) > 3 and len(window[-1]) > 3 and q[3] != window[-1][3])
            if window and (changed_floor or q[0] - window[-1][0] > 5 or math.hypot(q[1] - window[0][1], q[2] - window[0][2]) > 800):
                if len(window) >= 4 and window[-1][0] - window[0][0] >= 8:
                    pos = locate(window[0][0], pull)
                    if pos:
                        # Ignore repeated jitter stops in the same area.
                        if not stops or math.hypot(pos["x"] - stops[-1]["x"], pos["y"] - stops[-1]["y"]) > 1200:
                            stops.append({"kind": "stop", "t": window[0][0], "until": window[-1][0], **pos})
                window = []
            window.append(q)

        steps = sorted(batches + stops, key=lambda s: (s["t"], s["kind"] == "stop"))
        # Keep sampled polylines as separate segments, including discontinuities.
        segments, segment = [], []
        for q in samples:
            pos = locate(q[0], pull)
            if not pos or (segment and (q[0] - segment[-1]["t"] > 5 or pos["map"] != segment[-1]["map"])):
                if len(segment) > 1:
                    segments.append(segment)
                segment = []
            if pos:
                segment.append({"t": q[0], **pos})
        if len(segment) > 1:
            segments.append(segment)
        result["pulls"].append({"id": pull["id"], "start": lo, "end": hi, "steps": steps,
                                "segments": segments, "observed": len(own)})
        prior.extend(own)
    return result


def api_mplus_mdt(code, fight_id):
    """Build an importable MDT route string for this run using the locally installed MDT data."""
    data = api_mplus(code, fight_id)
    mdt_dir = mdt_export.find_mdt_dir()
    if not mdt_dir:
        raise WclError(translate("没有找到已安装的 MDT 插件。请确认 World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools 存在，"
                                "或设置环境变量 WCL_MDT_PATH 指向该目录。",
                                "Mythic Dungeon Tools was not found. Check World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools "
                                "or set WCL_MDT_PATH to that directory."))
    annotation_error = None
    try:
        route = api_mplus_paths(code, fight_id).get("tankRoute")
    except WclError as exc:
        route = None
        annotation_error = str(exc)
    try:
        result = mdt_export.build_route(mdt_dir, data["run"], data["pulls"], code, fight_id, tank_route=route)
    except mdt_export.MdtError as e:
        raise WclError(str(e))
    result.pop("preset", None)
    if annotation_error:
        result["annotation_notice"] = translate(f"路线已生成，接怪标记暂未导出：{annotation_error}", f"Route generated without pull annotations: {annotation_error}")
    result["mdt_path"] = str(mdt_dir)
    return result


def _extract_paths(code, fight, meta):
    start, end = fight["start_time"], fight["end_time"]
    tag = f".{fight['id']}."
    players = {p["id"] for p in meta["friendlies"] if tag in (p.get("fights") or "") and p.get("type") in CLASS_TYPES}
    enemies = {e["id"]: e for e in meta["enemies"]}
    tracks = {pid: {} for pid in players}
    mobs = {}
    contacts, deaths = {}, {}
    owners = {p["id"]: p.get("petOwner") for p in meta.get("friendlyPets", [])}
    seg = start
    while seg <= end:
        seg_end = min(seg + REPLAY_SEGMENT_MS - 1, end)
        data = wcl.get_json(code, f"/reports/replaysegment/{code}/{fight['boss']}/{seg}/{seg_end}/")
        for ev in data.get("events", []):
            t = round((ev["timestamp"] - start) / 1000, 1)
            source, target = ev.get("sourceID"), ev.get("targetID")
            source_player = owners.get(source, source)
            target_player = owners.get(target, target)
            side, who = None, None
            if ev.get("type") in ("damage", "miss", "absorbed"):
                if source_player in players and target in enemies and not ev.get("targetIsFriendly"):
                    side, who = "target", source_player
                elif target_player in players and source in enemies and not ev.get("sourceIsFriendly"):
                    side, who = "source", target_player
            elif ev.get("type") in ("cast", "begincast") and source in enemies and target_player in players:
                side, who = "source", target_player
            if side:
                actor = ev[f"{side}ID"]
                key = (actor, ev.get(f"{side}Instance", 1))
                if key not in contacts or t < contacts[key]["t"]:
                    ab = ev.get("ability") or {}
                    contacts[key] = {"actor": actor, "instance": key[1], "guid": enemies[actor].get("guid"),
                                     "name": enemies[actor].get("name", translate("未知怪物", "Unknown enemy")), "t": t,
                                     "who": who, "spell": ab.get("name", ""), "outgoing": side == "target"}
            if ev.get("type") == "death" and target in enemies and not ev.get("targetIsFriendly") and not ev.get("feign"):
                key = (target, ev.get("targetInstance", 1))
                deaths[key] = {"actor": target, "instance": key[1], "t": t}
            if "x" not in ev or ev.get("resourceActor") not in (1, 2):
                continue
            side = "source" if ev["resourceActor"] == 1 else "target"
            actor = ev.get(f"{side}ID")
            if actor in players:
                tracks[actor].setdefault(int(t // PATH_STEP_SEC), (t, ev["x"], ev["y"], ev.get("mapID")))
            elif actor in enemies and not ev.get(f"{side}IsFriendly"):
                key = (actor, ev.get(f"{side}Instance", 1))
                m = mobs.get(key)
                if m is None:
                    mobs[key] = {"actor": actor, "instance": key[1], "guid": enemies[actor].get("guid"), "t": t,
                                 "x": ev["x"], "y": ev["y"], "map": ev.get("mapID"),
                                 "lastT": t, "lx": ev["x"], "ly": ev["y"]}
                else:
                    m.update(lastT=t, lx=ev["x"], ly=ev["y"])
        seg = seg_end + 1
    return {
        "step": PATH_STEP_SEC,
        "tracks": {pid: [list(v) for _, v in sorted(pts.items())] for pid, pts in tracks.items()},
        "mobs": sorted(mobs.values(), key=lambda m: m["t"]),
        "contacts": sorted(contacts.values(), key=lambda c: c["t"]),
        "enemyDeaths": sorted(deaths.values(), key=lambda d: d["t"]),
    }


CLASS_TYPES = {"Warrior", "Paladin", "Hunter", "Rogue", "Priest", "DeathKnight", "Shaman", "Mage",
               "Warlock", "Monk", "Druid", "DemonHunter", "Evoker"}


def _spread(arr, t0, length, rate):
    t1 = t0 + length
    sec = int(max(t0, 0))
    while sec < len(arr) and sec < t1:
        overlap = min(t1, sec + 1) - max(t0, sec)
        if overlap > 0:
            arr[sec] += rate * overlap
        sec += 1


# ---------- icons ----------

ICON_DIR = CACHE / "icons"
ICON_NAME_RE = re.compile(r"[a-z0-9_-]{1,80}")
# Wowhead 的图片域名（wow.zamimg.com）对中国大陆的访问直接返回 403，所以优先用
# WCL 自己的素材域名和暴雪的图标 CDN，Wowhead 只作为最后的兜底。
ICON_UPSTREAMS = (
    "https://assets.rpglogs.com/img/warcraft/abilities/{name}.jpg",
    "https://render.worldofwarcraft.com/us/icons/56/{name}.jpg",
    "https://wow.zamimg.com/images/wow/icons/medium/{name}.jpg",
)
ICON_RETRY_SEC = 300
ICON_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56" width="56" height="56">'
    b'<rect width="56" height="56" rx="9" fill="#23272e"/>'
    b'<text x="28" y="39" text-anchor="middle" font-family="Arial, sans-serif"'
    b' font-size="30" font-weight="700" fill="#5f6773">?</text></svg>'
)
_icon_locks_guard = threading.Lock()
_icon_locks = {}
_icon_misses = {}


def normalize_icon(name):
    """把 WCL / Wowhead 的图标引用整理成安全的 CDN 文件名。"""
    slug = (name or "").strip().lower()
    slug = re.sub(r"^custom-icon-", "", slug)
    slug = re.sub(r"\.(?:jpe?g|png|tga|gif|webp|bmp)$", "", slug)
    return slug if ICON_NAME_RE.fullmatch(slug) else ""


def icon_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _icon_lock(slug):
    with _icon_locks_guard:
        return _icon_locks.setdefault(slug, threading.Lock())


def _download_icon(slug):
    for template in ICON_UPSTREAMS:
        try:
            r = requests.get(template.format(name=slug), timeout=15, impersonate="chrome")
        except Exception:
            continue
        if (r.status_code == 200 and r.headers.get("content-type", "").startswith("image/")
                and len(r.content) > 128):
            return r.content
    return None


def icon_bytes(name):
    """图标字节；全部图源都失败时返回 ``None``，由调用方给出占位图。

    第一张图从 CDN 取回后落盘到 ``cache/icons/``，之后完全离线可用。
    """
    slug = normalize_icon(name)
    if not slug:
        return None
    path = ICON_DIR / f"{slug}.jpg"
    try:
        if path.exists():
            return path.read_bytes()
    except OSError:
        pass
    missed = _icon_misses.get(slug)
    if missed and time.time() - missed < ICON_RETRY_SEC:
        return None
    with _icon_lock(slug):
        try:
            if path.exists():
                return path.read_bytes()
        except OSError:
            pass
        data = _download_icon(slug)
        if not data:
            _icon_misses[slug] = time.time()
            return None
        _icon_misses.pop(slug, None)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
        return data


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        token = LANGUAGE.set(normalize_language(q.get("lang")))
        try:
            self._dispatch(url, q)
        finally:
            LANGUAGE.reset(token)

    def _dispatch(self, url, q):
        try:
            if url.path == "/":
                return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/report":
                return self._json(api_report(q.get("link", "")))
            if url.path == "/api/fight":
                return self._json(api_fight(q["code"], int(q["fight"])))
            if url.path == "/mplus":
                return self._send(200, (ROOT / "mplus.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/i18n.js":
                return self._send(200, (ROOT / "i18n.js").read_bytes(), "application/javascript; charset=utf-8")
            if url.path == "/api/mplus/runs":
                return self._json(api_mplus_list(q.get("link", "")))
            if url.path == "/api/mplus/paths":
                return self._json(api_mplus_paths(q["code"], int(q["fight"])))
            if url.path == "/api/mplus/mdt":
                return self._json(api_mplus_mdt(q["code"], int(q["fight"])))
            if url.path == "/api/mplus":
                return self._json(api_mplus(q["code"], int(q["fight"])))
            m = re.fullmatch(r"/api/map/(\d+)\.png", url.path)
            if m:
                return self._send(200, map_image(int(m.group(1))), "image/png", cache=True)
            m = re.fullmatch(r"/api/icon/([A-Za-z0-9_.-]{1,120})", url.path)
            if m:
                data = icon_bytes(m.group(1))
                if data:
                    return self._send(200, data, icon_mime(data), cache=True)
                # 占位图不缓存，等 CDN 恢复后刷新页面就能拿到真图。
                return self._send(200, ICON_PLACEHOLDER, "image/svg+xml", cache_control="no-store")
            self._send(404, b"not found", "text/plain")
        except WclError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": translate(f"内部错误：{type(e).__name__}: {e}", f"Internal error: {type(e).__name__}: {e}")}, 500)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status, body, ctype, cache=False, cache_control=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        elif cache:
            self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


def serve(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    print(f"WCL 治疗作业 / Healing Playbook: http://{HOST}:{PORT}")
    serve().serve_forever()
