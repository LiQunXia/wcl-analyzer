# 治疗作业（WCL 日志分析）

这是一个本地运行的 Warcraft Logs 治疗技能时间轴分析工具。输入公开报告链接，选择首领战斗后，页面会展示团队承伤、首领技能、治疗大招、死亡和阶段，并生成可复制的 MRT 备忘。界面和错误提示以中文为主。

## 大秘境复盘（`/mplus`）

打开 <http://127.0.0.1:8000/mplus>，粘贴一份大秘境日志（例如 `https://cn.warcraftlogs.com/reports/XGdhCgWHtPpa9w6J?fight=5`）。页面提供：

- **路线地图**：按楼层绘制每一波的位置（编号）、波次顺序、怪物被接触时的位置及每波范围、坦克的真实移动轨迹和死亡位置；支持滚轮缩放、拖动，以及按时间回放全队位置（空格播放，←/→ 切换波次）。
- **全程时间轴**：每一波的起止、首领波、嗜血窗口与死亡。
- **逐波拆解**：本波怪物（击杀数 / 接触数、小怪值占比）、进度、用时，每个队员在波内的爆发 / 减伤 / 爆发药 / 治疗药 / 打断 / 嗜血 / 战复时间线（含开怪前 12 秒），以及本波 DPS、HPS、承伤。
- **技能矩阵与全程总览**：「人 × 波次」的爆发或减伤分布、每人整场的技能次数与爆发药波次，以及自动生成的复盘要点。
- **导出 MDT 路线**：点击地图工具栏的「导出 MDT」，生成 `!~MDT2~` 导入字符串，可直接粘贴到 Mythic Dungeon Tools 的路线导入框；路线会按 WCL 波次与进度匹配 MDT 的怪物组。

数据来源：波次、怪物与楼层来自 WCL 报告元数据中的 `dungeonPulls` / `dungeonReplay`；击杀与进度来自敌方死亡事件和 `npcCountMap`；技能来自施法图表并按 `mplus_spells.json` 分类（药水按名称识别）；走位来自 WCL 战斗回放（`/reports/replaysegment`，每 4 分钟约 40MB，首次读取约 30 秒，提取后的结果缓存在 `cache/<code>/<fight>_paths.json`）。地图图片经 `/api/map/<id>.png` 代理并缓存到 `cache/maps/`。MDT 导出直接读取本机已安装的 `World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools` 地下城数据，不随项目分发插件数据；如插件在非标准路径，可设置环境变量 `WCL_MDT_PATH` 指向插件目录。

## 快速启动

首次运行时，在项目目录创建虚拟环境并安装依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

之后在项目目录执行：

```sh
./start
```

也可以从任意目录执行 `~/Desktop/wcl/start`。打开 <http://127.0.0.1:8000>；按 `Ctrl+C` 停止。启动脚本会找到项目目录并使用 `.venv` 中的 Python。服务只监听本机的 `127.0.0.1:8000`，端口由 `server.py` 的 `PORT` 决定，可用环境变量 `WCL_HOST` / `WCL_PORT` 覆盖。

## 分享给别人（Windows 双击版）

对方不需要装 Python、不需要命令行：把打包出来的单个 `WCL-Analyzer.exe` 发过去，双击就会起服务并自动打开浏览器。给使用者的说明见 [docs/使用说明-Windows.md](docs/使用说明-Windows.md)。

### 两种出包方式

**A. 本地 Windows 机器打包**（最简单，五分钟）

```bat
build.bat
```

产物是 `dist\WCL-Analyzer.exe`。脚本会在 `.venv-build` 里装依赖，不污染开发用的 `.venv`。

**B. GitHub Actions 自动打包**（不用自己有 Windows 机器）

推送一个 `v*` 标签，或在 Actions 页面手动触发 `build-windows`：

```sh
git tag v1.0.0 && git push origin v1.0.0
```

workflow 会在 `windows-latest` 上用 Python 3.12 构建，把 exe 传到 Actions artifact，并在打了标签时自动附加到 Release。把 Release 链接发给朋友即可。

### 打包相关的约定

- 入口是 `launcher.py`：挑可用端口（8000 被占用会自动换）、起服务、打开浏览器；`server.serve()` 只负责监听。
- `wcl.spec` 用 `collect_all("curl_cffi")` 把原生 libcurl 一起打进去，`index.html` / `mplus.html` / `cooldowns.json` / `mplus_spells.json` 作为 datas 打进包。
- 冻结运行时 `server._resource_dir()` 从 `sys._MEIPASS` 读页面，`server._data_dir()` 把缓存写到 `%LOCALAPPDATA%\WCLAnalyzer`（macOS/Linux 是 `~/WCLAnalyzer`），避免写只读目录；也可以用 `WCL_DATA_DIR` 指定。
- 用 onefile + console：黑色窗口就是"退出按钮"，也方便看到报错。想改无窗口版把 `wcl.spec` 里 `console=True` 换成 `False`。
- exe 没有代码签名，Windows SmartScreen 会拦一次，需要点"更多信息 → 仍要运行"；这和程序本身无关。

## 给 agent 的代码地图

| 文件 | 职责 |
| --- | --- |
| `server.py` | HTTP 服务、WCL 数据获取与缓存、战斗数据分析、JSON 接口。 |
| `index.html` | 整个前端：页面结构、样式、请求、图表、筛选和 MRT 备忘生成；无需构建。 |
| `cooldowns.json` | 按治疗专精列出技能 ID；`raid` 为团队大招，`external` 为单体保命技能。 |
| `mplus.html` | 大秘境复盘页面（路线地图、波次、技能时间线、矩阵）；无需构建。 |
| `mplus_spells.json` | 大秘境复盘的技能分类：各职业 `offensive`/`defensive`，以及 `_global` 中的打断、战复、嗜血、药水、种族技能。 |
| `mdt_export.py` | 读取本机 MDT 插件数据，把 WCL 波次匹配成 MDT 怪物组，并编码为 `!~MDT2~` 导入字符串。 |
| `tests/` | `unittest` 单元测试，运行 `.venv/bin/python -m unittest discover -s tests`。 |
| `requirements.txt` | Python 依赖，目前仅有 `curl_cffi`。 |
| `start` | 从任意工作目录启动项目的本地服务（开发用，不打开浏览器）。 |
| `launcher.py` | 桌面版入口：选端口、起服务、自动开浏览器；打包后的 exe 跑的就是它。 |
| `wcl.spec` | PyInstaller 打包配置（onefile，含页面资源与 curl_cffi 原生库）。 |
| `build.bat` | Windows 本地一键打包脚本。 |
| `.github/workflows/build.yml` | 在 GitHub Actions 上构建 Windows exe 并发布到 Release。 |
| `docs/使用说明-Windows.md` | 发给使用者的说明（双击、SmartScreen、缓存位置、MDT 路径）。 |
| `cache/` | 运行时缓存的 WCL 响应与 Wowhead 技能名称、图标；已被 Git 忽略。 |
| `AGENTS.md` | 仓库约定、代码风格及修改后的验证要求。 |

大秘境接口：`GET /api/mplus/runs?link=...` 列出日志中的大秘境；`GET /api/mplus?code=...&fight=...` 返回 `run`、`maps`、`players`、`pulls`（每波怪物、进度、施法、死亡、每人数据）、`deaths`、`lust`、`heroism`；`GET /api/mplus/paths?code=...&fight=...` 返回队员轨迹 `tracks` 与怪物位置 `mobs`；`GET /api/mplus/mdt?code=...&fight=...` 返回可直接导入 MDT 的 `string`、匹配到的波次数与预计进度；`GET /api/map/<id>.png` 返回地图图片。

主要数据流：浏览器调用 `GET /api/report?link=...` 解析 WCL 链接并取得首领战斗列表；选择战斗后调用 `GET /api/fight?code=...&fight=...`。服务从 WCL 获取战斗人员、承伤及双方施法数据，按 `cooldowns.json` 匹配治疗技能，计算伤害压力与首领技能关联，再用 Wowhead 补充中文技能名。前端根据返回的 `fight`、`healers`、`damage`、`casts`、`bossCasts`、`bossAbilities`、`deaths`、`phases`、`heroism` 渲染页面和 MRT 备忘。

`index.html` 从 CDN 加载 ECharts 和 Wowhead tooltip 脚本；分析真实报告和显示这些内容需要网络。WCL 的公开报告也可能因访问限制或人机验证而请求失败。已结束战斗的图表响应会缓存在 `cache/`，报告元数据在内存中缓存 5 分钟；进行中的战斗不会持久化图表响应。

## 修改与验证

- 改数据获取、分析或接口：看 `server.py`；改展示与交互：看 `index.html`；增减治疗技能：改 `cooldowns.json` 中的数字 ID 与 `raid`/`external` 分类。
- Python 语法检查：`./.venv/bin/python -m py_compile server.py`。
- 冷却目录检查：`./.venv/bin/python -m json.tool cooldowns.json >/dev/null`。
- 行为改动：运行 `./start`，用公开 WCL 报告选择一场战斗，检查摘要、时间轴、MRT 备忘和浏览器控制台；接口改动还应检查对应的 `/api/report` 或 `/api/fight` 响应。

单元测试：`.venv/bin/python -m unittest discover -s tests`（离线，不访问 WCL）。没有构建步骤。更多代码风格与提交约定见 [`AGENTS.md`](AGENTS.md)。
