"""Export a Warcraft Logs Mythic+ run as an importable Mythic Dungeon Tools route.

The addon's current export format is ``!~MDT2~`` + Base64(RawDeflate(CBOR(preset))).
Dungeon/enemy data is read from the locally installed MDT addon, so no addon data is
bundled or redistributed with this project.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import math
import os
import re
import sys
import zlib
from pathlib import Path

PREFIX = "!~MDT2~"

COMMON_MDT_PATHS = (
    "/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
    "~/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
    "~/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
    "~/Games/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
    "C:/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
    "C:/Program Files/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools",
)


class MdtError(Exception):
    pass


def wow_addon_dirs() -> list[str]:
    """WoW ``_retail_`` folders, including installs on a non-default Windows drive.

    The registry is authoritative on Windows, so a game installed at
    ``D:/Games/World of Warcraft`` is found without asking the user.
    """
    found: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            return found
        for key in (
            r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
            r"SOFTWARE\Blizzard Entertainment\World of Warcraft",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                    install, _ = winreg.QueryValueEx(handle, "InstallPath")
            except OSError:
                continue
            found.append(str(Path(install) / "_retail_"))
    return found


def find_mdt_dir() -> Path | None:
    """Find the installed MDT addon. An explicit path can be provided with WCL_MDT_PATH."""
    discovered = [
        str(Path(base) / "Interface/AddOns/MythicDungeonTools") for base in wow_addon_dirs()
    ]
    candidates = [
        os.environ.get("WCL_MDT_PATH"),
        os.environ.get("MDT_PATH"),
        *discovered,
        *COMMON_MDT_PATHS,
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.name.lower() == "mythicdungeontools" and path.is_dir():
            return path
        nested = path / "MythicDungeonTools"
        if nested.is_dir():
            return nested
    return None


# ---------- Lua data parser (only the subset used by MDT dungeon files) ----------

class _LuaParser:
    def __init__(self, text: str, pos: int = 0):
        self.text = text
        self.pos = pos

    def _skip(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in " \t\r\n,":
                self.pos += 1
            elif text.startswith("--", self.pos):
                nl = text.find("\n", self.pos)
                self.pos = n if nl < 0 else nl + 1
            else:
                break

    def parse_table(self) -> dict:
        self._skip()
        if self.text[self.pos] != "{":
            raise MdtError("MDT 数据格式异常：需要 Lua table")
        self.pos += 1
        out: dict = {}
        index = 0
        while True:
            self._skip()
            if self.pos >= len(self.text):
                raise MdtError("MDT 数据格式异常：Lua table 未闭合")
            if self.text[self.pos] == "}":
                self.pos += 1
                return out
            if self.text[self.pos] == "[":
                self.pos += 1
                self._skip()
                key = self._parse_scalar()
                self._skip()
                if self.text[self.pos] != "]":
                    raise MdtError("MDT 数据格式异常：缺少 ]")
                self.pos += 1
                self._skip()
                if self.text[self.pos] != "=":
                    raise MdtError("MDT 数据格式异常：缺少 =")
                self.pos += 1
                out[key] = self._parse_value()
                continue
            # Bare identifier key, e.g. normal = 655 (not used inside dungeonEnemies).
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", self.text[self.pos:])
            if m:
                key = m.group(0).split("=")[0].strip()
                self.pos += m.end()
                out[key] = self._parse_value()
                continue
            index += 1
            out[index] = self._parse_value()

    def _parse_value(self):
        self._skip()
        ch = self.text[self.pos]
        if ch == "{":
            return self.parse_table()
        if ch in "\"'":
            return self._parse_string()
        m = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", self.text[self.pos:])
        if m:
            raw = m.group(0)
            self.pos += len(raw)
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        for word, value in (("true", True), ("false", False), ("nil", None)):
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return value
        raise MdtError(f"MDT 数据格式异常：无法解析 {self.text[self.pos:self.pos + 30]!r}")

    def _parse_scalar(self):
        m = re.match(r"-?\d+(?:\.\d+)?", self.text[self.pos:])
        if m:
            raw = m.group(0)
            self.pos += len(raw)
            return int(raw)
        if self.text[self.pos] in "\"'":
            return self._parse_string()
        raise MdtError("MDT 数据格式异常：不支持的 table key")

    def _parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                return "".join(out)
            if ch == "\\":
                self.pos += 1
                esc = self.text[self.pos]
                mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
                if esc.isdigit():
                    digits = self.text[self.pos:self.pos + 3]
                    m = re.match(r"\d{1,3}", digits)
                    out.append(chr(int(m.group(0))))
                    self.pos += len(m.group(0))
                    continue
                out.append(mapping.get(esc, esc))
                self.pos += 1
                continue
            out.append(ch)
            self.pos += 1
        raise MdtError("MDT 数据格式异常：字符串未闭合")


def _read_text(path: Path) -> str:
    return path.read_text("utf-8", errors="replace")


def _locale_names(mdt_dir: Path) -> dict[str, set[str]]:
    names: dict[str, set[str]] = collections.defaultdict(set)
    for locale in ("zhCN", "enUS", "zhTW"):
        path = mdt_dir / "Locales" / f"{locale}.lua"
        if not path.exists():
            continue
        for key, value in re.findall(r'L\["([^"]+)"\]\s*=\s*"((?:[^"\\]|\\.)*)"', _read_text(path)):
            names[key].add(value.replace('\\"', '"').replace("\\\\", "\\"))
    return names


def _catalog(mdt_dir: Path) -> list[dict]:
    locales = _locale_names(mdt_dir)
    catalog = []
    for sub in sorted(mdt_dir.iterdir()):
        if not sub.is_dir() or sub.name in {"Locales", "Modules", "Core", "Textures", "libs", "AceGUIWidgets"}:
            continue
        for path in sorted(sub.glob("*.lua")):
            text = _read_text(path)
            m = re.search(r"local\s+dungeonIndex\s*=\s*(\d+)", text)
            if not m:
                continue
            index = int(m.group(1))
            key = re.search(r'MDT\.dungeonList\[dungeonIndex\]\s*=\s*L\["([^"]+)"\]', text)
            english = re.search(r'englishName\s*=\s*"([^"]*)"', text)
            names = {english.group(1)} if english else set()
            if key:
                names.add(key.group(1))
                names.update(locales.get(key.group(1), set()))
            catalog.append({"index": index, "path": path, "key": key.group(1) if key else "",
                            "names": {n for n in names if n}})
    return catalog


def _match_dungeon(mdt_dir: Path, run_name: str) -> dict:
    catalog = _catalog(mdt_dir)
    wanted = re.sub(r"\s+", "", run_name or "").casefold()
    for entry in catalog:
        if any(re.sub(r"\s+", "", n).casefold() == wanted for n in entry["names"]):
            return entry
    for entry in catalog:
        if any(wanted and (wanted in re.sub(r"\s+", "", n).casefold()) for n in entry["names"]):
            return entry
    raise MdtError(f"MDT 里没有找到与「{run_name}」对应的地下城，请确认 MDT 已更新。")


def _load_enemies(path: Path) -> dict:
    text = _read_text(path)
    m = re.search(r"MDT\.dungeonEnemies\s*\[\s*dungeonIndex\s*\]\s*=\s*\{", text)
    if not m:
        raise MdtError(f"无法从 {path.name} 读取 MDT 怪物数据。")
    parser = _LuaParser(text, m.end() - 1)
    return parser.parse_table()


def _build_groups(enemies: dict) -> list[dict]:
    groups: dict = {}
    for enemy_idx, enemy in enemies.items():
        if not isinstance(enemy_idx, int) or not isinstance(enemy, dict):
            continue
        npc = enemy.get("id")
        if not npc:
            continue
        npc = int(npc)
        clones = enemy.get("clones") or {}
        for clone_idx, clone in clones.items():
            if not isinstance(clone_idx, int) or not isinstance(clone, dict):
                continue
            g = clone.get("g")
            key = int(g) if g is not None else 1000000 + enemy_idx * 100 + clone_idx
            group = groups.setdefault(key, {
                "key": key, "npcs": collections.Counter(), "clones": [],
                "boss": False, "order": len(groups),
            })
            group["npcs"][npc] += 1
            group["clones"].append((enemy_idx, clone_idx, npc, int(enemy.get("count") or 0)))
            if enemy.get("isBoss"):
                group["boss"] = True
    result = sorted(groups.values(), key=lambda gr: gr["key"])
    for i, group in enumerate(result):
        group["order"] = i
        group["force"] = sum(c for *_, c in group["clones"])
        group["size"] = sum(group["npcs"].values())
    return result


def _pull_target(pull: dict) -> collections.Counter:
    target = collections.Counter()
    for enemy in pull.get("enemies") or []:
        guid = enemy.get("guid")
        if guid is None:
            continue
        count = max(1, int(enemy.get("engaged") or 0), int(enemy.get("killed") or 0))
        target[int(guid)] += count
    return target


def _score_group(target: collections.Counter, group: dict) -> float:
    coverage = sum(min(target.get(npc, 0), count) for npc, count in group["npcs"].items())
    surplus = sum(max(0, count - target.get(npc, 0)) for npc, count in group["npcs"].items())
    return coverage * 3 - surplus * 0.8


def _assign(groups: list[dict], pulls: list[dict], required: int) -> list[list[int]]:
    """Assign MDT groups to WCL pulls.

    Boss pulls are anchored to the MDT group containing that boss. Trash pulls consume
    the remaining groups in MDT order, using WCL's progress percentage as the force
    budget. This keeps the exported route in dungeon order instead of jumping between
    identical-looking packs; the resulting route is a close approximation and can be
    adjusted in MDT.
    """
    used: set[int] = set()
    assignments: list[list[int]] = []
    cursor = 0
    total_clones = collections.Counter()
    for group in groups:
        total_clones.update(group["npcs"])

    for pull in pulls:
        target = _pull_target(pull)
        target = collections.Counter({npc: min(count, total_clones[npc]) for npc, count in target.items()})
        boss_npcs = {int(e["guid"]) for e in pull.get("enemies") or [] if e.get("boss") and e.get("guid") is not None}
        selected: list[int] = []

        if boss_npcs:
            for idx, group in enumerate(groups):
                if idx in used or not group["boss"]:
                    continue
                if any(npc in group["npcs"] for npc in boss_npcs):
                    selected.append(idx)
                    used.add(idx)
                    break
            if not selected:
                # Boss missing from MDT: fall back to the closest matching pack.
                best = None
                for idx, group in enumerate(groups):
                    if idx in used:
                        continue
                    score = _score_group(target, group)
                    if score > 0 and (best is None or score > best[0]):
                        best = (score, idx)
                if best:
                    selected.append(best[1])
                    used.add(best[1])
            assignments.append(selected)
            continue

        target_force = float(pull.get("forcesPct") or 0) / 100 * required
        if target_force <= 0:
            # A zero-progress pull can still contain a mini-boss/rare. Match one pack by composition.
            best = None
            for idx, group in enumerate(groups):
                if idx in used or group["boss"]:
                    continue
                score = _score_group(target, group)
                if score > 0 and (best is None or score > best[0]):
                    best = (score, idx)
            if best:
                selected.append(best[1])
                used.add(best[1])
            assignments.append(selected)
            continue

        force = 0
        while cursor < len(groups):
            group = groups[cursor]
            if cursor in used or group["boss"]:
                cursor += 1
                continue
            if force >= target_force:
                break
            # Once most of the budget is covered, don't add a pack that would overshoot too much,
            # and don't jump to an unrelated optional pack.
            overlap = sum(min(target.get(npc, 0), count) for npc, count in group["npcs"].items())
            if force >= target_force * 0.7 and (force + group["force"] > target_force * 1.3 or
                                                  (overlap == 0 and force > 0)):
                break
            selected.append(cursor)
            used.add(cursor)
            force += group["force"]
            cursor += 1
        assignments.append(selected)

    return assignments


def _route_objects(tank_route, pulls, assignments, groups, enemies):
    """MDT-native notes and schematic join arrows, never uncalibrated WCL coordinates.

    Notes are anchored to the approximate exported pull. Arrows are only possible
    when a batch's NPC species points to one of that pull's assigned MDT groups.
    The actual tank polyline/stay coordinates remain in the web map.
    """
    if not tank_route:
        return []
    objects = []
    by_id = {p["id"]: p for p in tank_route.get("pulls", [])}

    def position(group):
        clones = [enemies[ei]["clones"][ci] for ei, ci, *_ in group["clones"]]
        floors = {c.get("sublevel", 1) for c in clones}
        if len(floors) != 1 or any(c.get("x") is None or c.get("y") is None for c in clones):
            return None
        return (sum(c["x"] for c in clones) / len(clones), sum(c["y"] for c in clones) / len(clones), floors.pop())

    for number, (pull, indices) in enumerate(zip(pulls, assignments), 1):
        analysis = by_id.get(pull.get("id"))
        if not analysis or not analysis.get("steps") or not indices:
            continue
        positions = {idx: position(groups[idx]) for idx in indices}
        anchor = next((p for p in positions.values() if p), None)
        if anchor is None:
            continue
        lines = [f"WCL 第 {number} 波 · 接怪过程（示意位置）", "按日志首次交互分批；实际路径和停留位置见网页。"]
        previous = None
        for step in analysis["steps"]:
            offset = step["t"] - analysis["start"]
            time_label = f"{offset:+.0f}s"
            if step["kind"] == "stop":
                lines.append(f"{time_label} 坦克停留约 {step['until'] - step['t']:.0f}s（位置见网页）")
                continue
            counts = collections.Counter(c["name"] for c in step["members"])
            names = "、".join(f"{name}×{n}" for name, n in counts.items())
            action = "续接" if step.get("chain") else "接入"
            text = f"{time_label} {action} {step['label']}：{names}"
            if step.get("alive"):
                text += f"；至少 {step['alive']} 只旧怪仍存活"
            if step.get("recentDeaths"):
                text += "；此前8秒死亡：" + "、".join(dict.fromkeys(d["name"] for d in step["recentDeaths"]))
            lines.append(text)

            # A distinctive NPC must select a single group; don't guess among identical packs.
            candidates = set()
            for npc in {c.get("guid") for c in step["members"]}:
                matches = [idx for idx in indices if npc in groups[idx]["npcs"]]
                if len(matches) == 1:
                    candidates.add(matches[0])
            target = positions[next(iter(candidates))] if len(candidates) == 1 else None
            if target is None:
                previous = None
                continue
            x, y, floor = target
            objects.append({"n": True, "d": [round(x, 1), round(y, 1), floor, True,
                            f"WCL 第{number}波 {time_label} {action} {step['label']}（怪群位置示意）\n{names}"]})
            if previous and previous[2] == floor and math.hypot(x - previous[0], y - previous[1]) > 3:
                px, py, _ = previous
                objects.append({"d": [9, 1, floor, True, "f2994a" if step.get("chain") else "f6c85f", 0],
                                "l": [round(px, 1), round(py, 1), round(x, 1), round(y, 1)],
                                "t": [math.atan2(py - y, px - x)]})
            previous = target
        x, y, floor = anchor
        objects.append({"n": True, "d": [round(x + 10, 1), round(y + 10, 1), floor, True, "\n".join(lines)]})
    return objects


def build_route(mdt_dir: Path, run: dict, pulls: list[dict], code: str = "", fight_id: int = 0,
                tank_route: dict | None = None) -> dict:
    dungeon = _match_dungeon(mdt_dir, run.get("name") or "")
    enemies = _load_enemies(dungeon["path"])
    groups = _build_groups(enemies)
    if not groups:
        raise MdtError("MDT 怪物数据为空，无法生成路线。")
    required = int(run.get("countRequired") or 0)
    assignments = _assign(groups, pulls, required)

    route_pulls = []
    force = 0
    used_groups = 0
    for indices in assignments:
        pull: dict[int, list[int]] = {}
        for idx in indices:
            group = groups[idx]
            used_groups += 1
            force += group["force"]
            for enemy_idx, clone_idx, _npc, _count in group["clones"]:
                pull.setdefault(enemy_idx, []).append(clone_idx)
        if pull:
            route_pulls.append(pull)

    if not route_pulls:
        raise MdtError("没有匹配到任何 MDT 怪物，无法生成路线。")

    title = f"WCL {run.get('name') or dungeon['key']} +{run.get('level') or '?'}"
    uid = "WCL" + hashlib.sha1(f"{code}:{fight_id}".encode()).hexdigest()[:8]
    preset = {
        "objects": _route_objects(tank_route, pulls, assignments, groups, enemies),
        "difficulty": int(run.get("level") or 0),
        "week": 1,
        "value": {
            "currentDungeonIdx": int(dungeon["index"]),
            "selection": [],
            "currentPull": 1,
            "teeming": False,
            "riftOffsets": [],
            "pulls": route_pulls,
            "enemyAssignments": [],
            "currentSublevel": 1,
        },
        "text": title[:80],
        "mdi": {"freeholdJoined": False, "freehold": 1, "beguiling": 1},
        "uid": uid,
    }
    return {
        "preset": preset,
        "string": encode_mdt2(preset),
        "dungeon": dungeon["key"],
        "dungeon_index": int(dungeon["index"]),
        "pulls": len(route_pulls),
        "groups": used_groups,
        "force": force,
        "required": required,
        "title": title,
        "notes": sum(bool(obj.get("n")) for obj in preset["objects"]),
        "arrows": sum(bool(obj.get("t")) for obj in preset["objects"]),
        "annotation_notice": "MDT 注释与箭头按近似匹配的怪群绘制，仅示意接怪顺序；实际走位和停留位置请查看网页。",
    }


# ---------- MDT2 codec: CBOR -> raw DEFLATE -> Base64 ----------

def _cbor_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 0x100:
        return bytes([(major << 5) | 24, value])
    if value < 0x10000:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    if value < 0x100000000:
        return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")
    return bytes([(major << 5) | 27]) + value.to_bytes(8, "big")


def cbor_encode(value) -> bytes:
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + __import__("struct").pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _cbor_head(2, len(raw)) + raw
    if isinstance(value, (bytes, bytearray)):
        return _cbor_head(2, len(value)) + bytes(value)
    if isinstance(value, (list, tuple)):
        return _cbor_head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        if not value:
            # Blizzard SerializeCBOR resolves empty tables to an empty array.
            return b"\x80"
        if list(value.keys()) == list(range(1, len(value) + 1)):
            return _cbor_head(4, len(value)) + b"".join(cbor_encode(v) for v in value.values())
        out = [_cbor_head(5, len(value))]
        for key, item in value.items():
            if isinstance(key, int) and key >= 0:
                out.append(_cbor_head(0, key))
            elif isinstance(key, int):
                out.append(_cbor_head(1, -1 - key))
            else:
                raw = str(key).encode("utf-8")
                out.append(_cbor_head(2, len(raw)) + raw)
            out.append(cbor_encode(item))
        return b"".join(out)
    raise MdtError(f"无法编码 CBOR 类型：{type(value).__name__}")


def encode_mdt2(preset: dict) -> str:
    raw = cbor_encode(preset)
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(raw) + compressor.flush()
    return PREFIX + base64.b64encode(deflated).decode("ascii")


def decode_mdt2(string: str) -> dict:
    if not string.startswith(PREFIX):
        raise MdtError("不是 MDT2 导入字符串")
    deflated = base64.b64decode(string[len(PREFIX):])
    raw = zlib.decompress(deflated, -15)
    return _cbor_decode(raw, [0])[0]


def _cbor_decode(data: bytes, pos: list[int]):
    initial = data[pos[0]]
    pos[0] += 1
    major, info = initial >> 5, initial & 0x1F
    if info < 24:
        value = info
    elif info == 24:
        value = data[pos[0]]
        pos[0] += 1
    elif info == 25:
        value = int.from_bytes(data[pos[0]:pos[0] + 2], "big")
        pos[0] += 2
    elif info == 26:
        value = int.from_bytes(data[pos[0]:pos[0] + 4], "big")
        pos[0] += 4
    elif info == 27:
        value = int.from_bytes(data[pos[0]:pos[0] + 8], "big")
        pos[0] += 8
    else:
        raise MdtError("不支持的 CBOR 数据")
    if major == 0:
        return value, pos
    if major == 1:
        return -1 - value, pos
    if major == 2:
        raw = data[pos[0]:pos[0] + value]
        pos[0] += value
        return raw.decode("utf-8"), pos
    if major == 4:
        return [_cbor_decode(data, pos)[0] for _ in range(value)], pos
    if major == 5:
        out = {}
        for _ in range(value):
            key = _cbor_decode(data, pos)[0]
            out[key] = _cbor_decode(data, pos)[0]
        return out, pos
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info == 22:
            return None, pos
        if info == 27:
            import struct
            return struct.unpack(">d", data[pos[0] - 8:pos[0]])[0], pos
    raise MdtError(f"不支持的 CBOR 类型：{major}")
