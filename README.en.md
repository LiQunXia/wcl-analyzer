# Healing Playbook

[中文](README.md) | **English**

Turn Warcraft Logs fights into a readable healing cooldown timeline, and break a Mythic+ run down pull by pull.

No Python install, no account, and logs stay on your computer. The interface supports Chinese and English.

## Download

| Platform | Download | How to open |
| --- | --- | --- |
| Windows 10/11 | [WCL-Analyzer.exe](https://github.com/LiQunXia/wcl-analyzer/releases/latest/download/WCL-Analyzer.exe) | Double-click |
| macOS (Apple silicon) | [`WCL-Analyzer-macos-arm64.zip`](https://github.com/LiQunXia/wcl-analyzer/releases/latest) in Releases | Unzip, then run `./WCL-Analyzer` in Terminal |
| macOS (Intel) | [`WCL-Analyzer-macos-x86_64.zip`](https://github.com/LiQunXia/wcl-analyzer/releases/latest) in Releases | Same as above |

Not sure which Mac you have? Open the Apple menu → “About This Mac” and check “Chip”. Choose `arm64` for Apple M-series, `x86_64` for Intel.

If Releases does not have a macOS build yet, see [Run from source](#run-from-source-optional).

## Three steps

1. **Open the app.** On Windows, double-click `WCL-Analyzer.exe`. On macOS, unzip the download and run `./WCL-Analyzer` in Terminal.
2. **Wait for the browser to open** at <http://127.0.0.1:8000>. Keep the console/Terminal window open — closing it quits the app.
3. **Paste a report link** into the input box, click “解析” (Analyze), then pick a fight.

Want to try it? Use this public log (the one in the screenshots): <https://cn.warcraftlogs.com/reports/qA96N4W2jwkKDv8c?fight=53>

Warcraft Logs may occasionally ask for human verification. A small window will open; click “I am a human and not a bot” yourself, and the analysis will continue automatically.

## Language

Choose **English** or **中文** from the language selector at the top right. Your choice is remembered across both review pages and applies to the interface, encounters, dungeons, enemies, ability names, Wowhead hover tooltips, and MRT/MDT export text. Character names keep their original spelling. Switching languages reloads the current report and fight.

Open `http://127.0.0.1:8000/?lang=en` or `/mplus?lang=en` to select English directly. Report data and spell names are cached separately for each language, so the first switch needs to fetch data again. Hover tooltips require access to Wowhead.

## Fight review

![Fight review: damage taken, boss abilities, healing cooldowns, and the MRT note](docs/images/analysis.png)

- Damage taken, boss abilities, Bloodlust/Heroism, deaths, and phases share one timeline.
- The app highlights boss abilities that call for a healing cooldown and shows the pressure afterwards.
- Click a healer to show only that healer's cooldowns.
- An MRT note is generated on the right; click “复制” (Copy) to paste it into the game.

## Mythic+ review

Click “大秘境复盘” (Mythic+ Review) at the top of the page, paste a Mythic+ report, and the run is broken down pull by pull.

![Mythic+ route map: pulls, enemies, movement, and deaths](docs/images/mplus-map.png)

**Route map:** Each pull's location, enemies, tank path, and deaths are drawn on the map. Zoom, pan, or replay the run; the full pull list is on the right. “只看本波” (current pull only) is on by default, keeping the selected pull's enemies and tank marks while other pulls become small clickable dots; turn it off for the whole-floor view.

![Pull breakdown: enemies, cooldown timeline, and cooldown matrix](docs/images/mplus-detail.png)

**Pull breakdown:** See which enemies were pulled, the progress and time for the pull, and every burst cooldown, defensive, combat potion, healing potion, and interrupt used by each player. A player × pull cooldown matrix follows below.

**MDT export:** Click “导出 MDT” (Export MDT) in the map toolbar to generate a `!~MDT2~` string. Paste it into the Mythic Dungeon Tools route import box. MDT must be installed locally.

## FAQ

| Symptom | What to do |
| --- | --- |
| Windows says “Windows protected your PC” | Click “更多信息” (More info) → “仍要运行” (Run anyway). The app is not code-signed; this is not a virus. |
| macOS says the app cannot be verified or is damaged | Open System Settings → Privacy & Security and click “仍要打开” (Open Anyway); or run `xattr -dr com.apple.quarantine WCL-Analyzer` in Terminal. |
| The browser did not open | Open <http://127.0.0.1:8000> manually. If port 8000 is in use, open the address printed in the console window. |
| WCL asks for human verification | Click “I am a human and not a bot” in the small window yourself. |
| The first analysis is slow | The first run has to fetch data from WCL (Mythic+ replay can take tens of seconds); later runs use the local cache. |
| Spell icons show as gray question marks | Icons are fetched from WCL/Blizzard and cached locally; refresh after Warcraft Logs is reachable. Wowhead images are blocked in mainland China, but that does not affect icons (only Wowhead hover tooltips). |
| MDT export cannot find the addon | Install Mythic Dungeon Tools. If it is in a non-default location, set `WCL_MDT_PATH` to the addon directory. |

All caches stay on your computer: `%LOCALAPPDATA%\WCLAnalyzer` on Windows and `~/WCLAnalyzer` on macOS. Deleting them is safe; the app will simply fetch data again.

Detailed install guides (in Chinese):

- [Windows guide](docs/使用说明-Windows.md)
- [macOS guide](docs/使用说明-macOS.md)

## Run from source (optional)

Python 3.10 or newer is required. From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start
```

Then open <http://127.0.0.1:8000>. On Windows, replace the last command with `.venv\Scripts\python server.py`.

See [AGENTS.md](AGENTS.md) for the code layout, tests, and packaging.

Run offline tests with `.venv/bin/python -m unittest discover -s tests`, including floor transitions, locale propagation, concurrent request/cache isolation, and localized MDT exports. Report-link boot and Analyze button regression tests require Node.js and are skipped when it is unavailable.
