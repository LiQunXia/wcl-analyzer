"""Refresh the compact MDT dungeon catalog from a MythicDungeonTools checkout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


class LuaDataError(ValueError):
    """Raised when an upstream Lua dungeon table cannot be read safely."""


class LuaTableParser:
    """Parse the static Lua table subset used by MDT dungeon data files."""

    def __init__(self, text: str, pos: int = 0):
        self.text = text
        self.pos = pos

    def _skip(self) -> None:
        text, size = self.text, len(self.text)
        while self.pos < size:
            if text[self.pos].isspace() or text[self.pos] == ",":
                self.pos += 1
                continue
            if text.startswith("--", self.pos):
                long_comment = re.match(r"--\[(=*)\[", text[self.pos:])
                if long_comment:
                    closer = "]" + long_comment.group(1) + "]"
                    start = self.pos + long_comment.end()
                    end = text.find(closer, start)
                    if end < 0:
                        raise LuaDataError("unclosed Lua long comment")
                    self.pos = end + len(closer)
                else:
                    end = text.find("\n", self.pos)
                    self.pos = size if end < 0 else end + 1
                continue
            break

    def parse_table(self) -> dict:
        self._skip()
        if self.pos >= len(self.text) or self.text[self.pos] != "{":
            raise LuaDataError("expected a Lua table")
        self.pos += 1
        table = {}
        array_index = 0

        while True:
            self._skip()
            if self.pos >= len(self.text):
                raise LuaDataError("unclosed Lua table")
            if self.text[self.pos] == "}":
                self.pos += 1
                return table

            if self.text[self.pos] == "[":
                self.pos += 1
                key = self._parse_scalar()
                self._skip()
                self._expect("]")
                self._skip()
                self._expect("=")
                table[key] = self._parse_value()
                continue

            named_key = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", self.text[self.pos:])
            if named_key:
                key = named_key.group(1)
                self.pos += named_key.end()
                table[key] = self._parse_value()
                continue

            array_index += 1
            table[array_index] = self._parse_value()

    def _expect(self, value: str) -> None:
        if self.pos >= len(self.text) or self.text[self.pos] != value:
            raise LuaDataError(f"expected {value!r} at offset {self.pos}")
        self.pos += 1

    def _parse_value(self):
        self._skip()
        if self.pos >= len(self.text):
            raise LuaDataError("unexpected end of Lua table")

        char = self.text[self.pos]
        if char == "{":
            return self.parse_table()
        if char in "\"'":
            return self._parse_quoted_string()
        if char == "[":
            long_string = re.match(r"\[(=*)\[", self.text[self.pos:])
            if long_string:
                closer = "]" + long_string.group(1) + "]"
                start = self.pos + long_string.end()
                end = self.text.find(closer, start)
                if end < 0:
                    raise LuaDataError("unclosed Lua long string")
                self.pos = end + len(closer)
                return self.text[start:end]

        number = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", self.text[self.pos:])
        if number:
            raw = number.group(0)
            self.pos += len(raw)
            return float(raw) if any(c in raw for c in ".eE") else int(raw)

        for word, value in (("true", True), ("false", False), ("nil", None)):
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return value

        snippet = self.text[self.pos:self.pos + 40]
        raise LuaDataError(f"unsupported Lua value at offset {self.pos}: {snippet!r}")

    def _parse_scalar(self):
        self._skip()
        if self.pos >= len(self.text):
            raise LuaDataError("unexpected end while reading a table key")
        if self.text[self.pos] in "\"'":
            return self._parse_quoted_string()
        number = re.match(r"-?\d+", self.text[self.pos:])
        if number:
            self.pos += len(number.group(0))
            return int(number.group(0))
        raise LuaDataError(f"unsupported Lua table key at offset {self.pos}")

    def _parse_quoted_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        result = []
        escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                   "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"'}

        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(result)
            if char != "\\":
                result.append(char)
                continue
            if self.pos >= len(self.text):
                raise LuaDataError("unfinished Lua string escape")
            escaped = self.text[self.pos]
            if escaped.isdigit():
                digits = re.match(r"\d{1,3}", self.text[self.pos:]).group(0)
                result.append(chr(int(digits)))
                self.pos += len(digits)
            else:
                result.append(escapes.get(escaped, escaped))
                self.pos += 1

        raise LuaDataError("unclosed Lua string")


def _read_locales(source: Path) -> dict[str, dict[str, str]]:
    locales: dict[str, dict[str, str]] = {}
    assignment = re.compile(r'L\["((?:[^"\\]|\\.)*)"\]\s*=\s*"((?:[^"\\]|\\.)*)"')
    for locale in ("enUS", "zhCN", "zhTW"):
        path = source / "Locales" / f"{locale}.lua"
        values: dict[str, str] = {}
        if path.is_file():
            for key, value in assignment.findall(path.read_text("utf-8", errors="replace")):
                key = key.replace(r'\"', '"').replace(r"\\", "\\")
                value = value.replace(r'\"', '"').replace(r"\\", "\\")
                values[key] = value
        locales[locale] = values
    return locales


def _source_dungeons(source: Path, locales: dict[str, dict[str, str]]) -> list[dict]:
    catalog = []
    seen_indices = set()

    for path in sorted(source.rglob("*.lua")):
        text = path.read_text("utf-8", errors="replace")
        index_match = re.search(r"local\s+dungeonIndex\s*=\s*(\d+)", text)
        table_match = re.search(
            r"MDT\.dungeonEnemies\s*\[\s*dungeonIndex\s*\]\s*=\s*\{", text
        )
        if not index_match or not table_match:
            continue

        index = int(index_match.group(1))
        if index in seen_indices:
            raise LuaDataError(f"duplicate dungeon index {index} in {path}")
        seen_indices.add(index)

        name_key_match = re.search(
            r'MDT\.dungeonList\s*\[\s*dungeonIndex\s*\]\s*=\s*L\["((?:[^"\\]|\\.)*)"\]',
            text,
        )
        english_match = re.search(r'englishName\s*=\s*"((?:[^"\\]|\\.)*)"', text)
        name_key = name_key_match.group(1) if name_key_match else ""
        english_name = english_match.group(1) if english_match else ""

        parser = LuaTableParser(text, table_match.end() - 1)
        raw_enemies = parser.parse_table()
        enemies = _compact_enemies(raw_enemies)
        if not enemies:
            continue

        names = []
        for candidate in (
            english_name,
            locales["enUS"].get(name_key, ""),
            name_key,
            locales["zhCN"].get(name_key, ""),
            locales["zhTW"].get(name_key, ""),
        ):
            if candidate and candidate not in names:
                names.append(candidate)
        if not names:
            names.append(path.stem)

        catalog.append({
            "index": index,
            "key": name_key or english_name or path.stem,
            "names": names,
            "enemies": enemies,
            "source_path": path.relative_to(source).as_posix(),
        })

    if len(catalog) < 4:
        raise LuaDataError(f"only found {len(catalog)} dungeon catalogs in {source}")
    return catalog


def _compact_enemies(raw_enemies: dict) -> dict[str, dict]:
    compact = {}
    for enemy_index, enemy in sorted(raw_enemies.items(), key=lambda item: int(item[0])):
        if not isinstance(enemy_index, int) or not isinstance(enemy, dict):
            continue
        if not enemy.get("id"):
            continue

        clones = {}
        for clone_index, clone in sorted((enemy.get("clones") or {}).items(), key=lambda item: int(item[0])):
            if not isinstance(clone_index, int) or not isinstance(clone, dict):
                continue
            compact_clone = {
                field: clone[field]
                for field in ("x", "y", "g", "sublevel")
                if clone.get(field) is not None
            }
            if compact_clone:
                clones[str(clone_index)] = compact_clone

        entry = {"id": int(enemy["id"]), "count": int(enemy.get("count") or 0)}
        if enemy.get("isBoss"):
            entry["isBoss"] = True
        entry["clones"] = clones
        compact[str(enemy_index)] = entry
    return compact


def _ordered_catalog(source_catalog: list[dict], existing: list[dict]) -> list[dict]:
    existing_by_index = {int(dungeon["index"]): dungeon for dungeon in existing}
    source_by_index = {int(dungeon["index"]): dungeon for dungeon in source_catalog}
    old_order = [int(dungeon["index"]) for dungeon in existing]
    new_indices = sorted(
        (index for index in source_by_index if index not in existing_by_index),
        key=lambda index: source_by_index[index]["source_path"],
    )

    result = []
    for index in [*old_order, *new_indices]:
        source_dungeon = source_by_index.get(index)
        if not source_dungeon:
            continue
        old_dungeon = existing_by_index.get(index, {})
        new_names = source_dungeon["names"]
        old_names = old_dungeon.get("names", [])
        names = [name for name in old_names if name in new_names]
        names.extend(name for name in new_names if name not in names)
        key = old_dungeon.get("key")
        if key not in names:
            key = source_dungeon["key"]

        result.append({
            "index": index,
            "key": key,
            "names": names,
            "enemies": source_dungeon["enemies"],
        })
    return result


def sync_catalog(source: Path, revision: str, output: Path, notice: Path) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("source revision must be a full lowercase Git commit SHA")

    existing = json.loads(output.read_text("utf-8"))
    if not isinstance(existing, list):
        raise ValueError(f"expected a JSON list in {output}")

    source_catalog = _source_dungeons(source, _read_locales(source))
    catalog = _ordered_catalog(source_catalog, existing)
    serialized = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if output.read_text("utf-8") == serialized:
        print("MDT dungeon data is already up to date.")
        return False

    notice_text = notice.read_text("utf-8")
    source_lines = re.findall(r"^Source revision: [0-9a-f]{40,64}$", notice_text, re.MULTILINE)
    if len(source_lines) != 1:
        raise ValueError(f"expected one source revision in {notice}")
    updated_notice = re.sub(
        r"^Source revision: [0-9a-f]{40,64}$",
        f"Source revision: {revision}",
        notice_text,
        count=1,
        flags=re.MULTILINE,
    )

    output.write_text(serialized, encoding="utf-8")
    notice.write_text(updated_notice, encoding="utf-8")
    print(f"Updated {len(catalog)} dungeon records from MDT {revision}.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="MythicDungeonTools checkout")
    parser.add_argument("--revision", required=True, help="full commit SHA of the source checkout")
    parser.add_argument("--output", type=Path, default=Path("mdt_dungeons.json"))
    parser.add_argument("--notice", type=Path, default=Path("licenses/MDT-DATA-NOTICE.txt"))
    args = parser.parse_args()

    try:
        sync_catalog(args.source, args.revision, args.output, args.notice)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"MDT data sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
