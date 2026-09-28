# Repository Guidelines

## Project Structure & Module Organization

This is a small Warcraft Logs analysis app with no build step. `server.py` runs the HTTP server, fetches and analyzes report data, and serves the UI. `index.html` contains the page, styles, and browser-side JavaScript. `cooldowns.json` lists healer spell IDs by specialization and category (`raid` or `external`). `requirements.txt` contains the Python dependency. Runtime responses and spell metadata go under `cache/`, which is ignored by Git. There is currently no `tests/` directory or separate asset pipeline.

## Build, Test, and Development Commands

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start
```

Open `http://127.0.0.1:8000` to use the app. The server binds to localhost on port 8000. Run `python -m py_compile server.py` for a quick Python syntax check and `python -m json.tool cooldowns.json >/dev/null` after editing the cooldown catalog. No build, lint, or automated test command is configured.

## Coding Style & Naming Conventions

Follow the existing style: four-space indentation and `snake_case` for Python functions and variables; two-space indentation and `camelCase` for browser JavaScript functions. Keep Python constants in `UPPER_SNAKE_CASE`. Put report fetching and analysis in `server.py`, UI behavior in `index.html`, and spell classification data in `cooldowns.json`. Preserve the existing Chinese user-facing labels and UTF-8 encoding. Add cooldown entries using numeric spell IDs and the established `raid`/`external` categories.

## Testing Guidelines

There is no test framework or coverage target yet. For behavior changes, start the server, load a public Warcraft Logs report, select a fight, and check the summary, cooldown timeline, and browser console. Exercise the affected `/api/report` or `/api/fight` endpoint when changing response logic. If adding automated tests, place them in `tests/` with `test_*.py` names and document the command needed to run them.

## Commit & Pull Request Guidelines

The short Git history contains `init` and a descriptive `Enhance UI and functionality` commit; it does not establish a strict format. Use a concise, action-oriented subject that names the main change. In pull requests, explain the behavior changed, include steps or a report link used to verify it, and add screenshots for UI changes. Keep generated `cache/` data, virtual environments, and local secrets out of commits.
