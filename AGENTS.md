# Repository Guidelines

## Project Structure & Module Organization

This is a small Warcraft Logs analysis app with no build step. `server.py` runs the HTTP server, fetches and analyzes report data, serves the UI, and proxies spell icons at `/api/icon/<name>` (trying WCL's asset host, then Blizzard's icon CDN, then Wowhead, and caching the bytes under `cache/icons/`) because Wowhead's image host returns 403 in mainland China. `wcl_verify.py` is the human-verification helper: it opens a system WebView in a child process so the user can click WCL's "I am a human" button, then returns the session cookies to `server.py`; `launcher.py` routes the packaged `--verify` child to it. `index.html` contains the page, styles, and browser-side JavaScript. `cooldowns.json` lists healer spell IDs by specialization and category (`raid` or `external`). `mdt_export.py` reads the locally installed Mythic Dungeon Tools addon and converts a Mythic+ run into an MDT `!~MDT2~` import string. `mplus.html` is the Mythic+ review page (served at `/mplus`), and `mplus_spells.json` classifies spells for it by class (`offensive`/`defensive`) plus `_global` lists (interrupt, rez, lust, potion, healPotion, racials). `requirements.txt` contains the Python dependencies (`curl_cffi` and `pywebview`). `launcher.py` is the desktop entry point (picks a free port, starts the server, opens the browser) and `wcl.spec` is the PyInstaller one-file packaging config; `build.bat` and `.github/workflows/build.yml` build the Windows exe, and the same workflow builds macOS arm64 and x86_64 binaries. Runtime responses and spell metadata go under `cache/`, which is ignored by Git (including `wcl_cookies.json`); packaged builds write them to `%LOCALAPPDATA%\WCLAnalyzer` on Windows (`~/WCLAnalyzer` on macOS/Linux) instead and read HTML/JSON assets from `sys._MEIPASS`. Offline unit tests live in `tests/`; there is no separate asset pipeline.

## Build, Test, and Development Commands

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start
```

Open `http://127.0.0.1:8000` to use the app. The server binds to localhost on port 8000 (override with `WCL_HOST` / `WCL_PORT`); `launcher.py` opens the browser and falls back to a free port. Run `python -m py_compile server.py launcher.py wcl_verify.py` for a quick Python syntax check and `python -m json.tool cooldowns.json >/dev/null` after editing the cooldown catalog.

To produce the shareable Windows executable (single file, no Python required on the target machine):

```bat
build.bat
```

or on any platform with PyInstaller installed: `python -m PyInstaller --noconfirm wcl.spec`. The output is `dist/WCL-Analyzer.exe` on Windows and `dist/WCL-Analyzer` on macOS (the arch of the build host). CI does the same on `windows-latest`, `macos-26` (arm64), and `macos-15-intel` (x86_64) for `v*` tags, packaging the macOS binaries as zips. There is no lint command.

## Coding Style & Naming Conventions

Follow the existing style: four-space indentation and `snake_case` for Python functions and variables; two-space indentation and `camelCase` for browser JavaScript functions. Keep Python constants in `UPPER_SNAKE_CASE`. Put report fetching and analysis in `server.py`, UI behavior in `index.html`, and spell classification data in `cooldowns.json`. Preserve the existing Chinese user-facing labels and UTF-8 encoding. Add cooldown entries using numeric spell IDs and the established `raid`/`external` categories.

## Testing Guidelines

Run `.venv/bin/python -m unittest discover -s tests` for the offline unit tests (no coverage target). For behavior changes, start the server, load a public Warcraft Logs report, select a fight, and check the summary, cooldown timeline, and browser console. Exercise the affected `/api/report`, `/api/fight`, or `/api/mplus*` endpoint when changing response logic; for Mythic+ changes, open `/mplus`, load a report, and check the map, pull detail, and matrix. If adding automated tests, place them in `tests/` with `test_*.py` names and document the command needed to run them.

## Commit & Pull Request Guidelines

The short Git history contains `init` and a descriptive `Enhance UI and functionality` commit; it does not establish a strict format. Use a concise, action-oriented subject that names the main change. In pull requests, explain the behavior changed, include steps or a report link used to verify it, and add screenshots for UI changes. Keep generated `cache/` data, virtual environments, and local secrets out of commits.
