# 分享给朋友（Windows 双击版）

这一页是写给**拿到程序的人**的，可以直接整段复制给对方。

## 三步用起来

1. 打开 <https://github.com/LiQunXia/wcl-analyzer/releases/latest>，下载 `WCL-Analyzer.exe`
   （单个文件，不用解压，不用注册 GitHub）。
2. 双击它。会弹出一个黑色窗口，**不要关它**，浏览器大约 1 秒后自动打开
   <http://127.0.0.1:8000>。
3. 把 Warcraft Logs 的报告链接粘进输入框，选择战斗即可。

用完之后，关掉那个黑色窗口就是退出。

## 如果弹出「WCL 人机验证」窗口

Warcraft Logs 有时会要求确认访问者不是机器人。这时程序会弹出一个小窗口，
**由你自己点击页面上的「I am a human and not a bot」**（程序不会替你点）。
验证通过后窗口会自动关闭，分析继续进行。这个窗口用的是 Windows 自带的
WebView2 组件，Windows 10/11 一般已经装好；如果提示打不开窗口，安装一下
[WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/) 即可。

验证结果会保存在 `%LOCALAPPDATA%\WCLAnalyzer\cache\wcl_cookies.json`，
一段时间内不需要重复验证。想重新验证时删掉这个文件即可。

## 第一次运行时可能遇到

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 蓝色窗口「Windows 已保护你的电脑」 | exe 没有购买代码签名证书 | 点「更多信息」→「仍要运行」 |
| 杀毒软件报毒 / 直接删掉 | PyInstaller 打包的程序常被误报 | 加入信任白名单，或改用 `--onedir` 版本 |
| 浏览器没自动打开 | 默认浏览器关联异常 | 手动打开 http://127.0.0.1:8000 |
| 提示端口被占用 | 8000 已被别的程序用了 | 程序会自动换端口，按黑色窗口里打印的地址打开 |
| 提示「无法打开验证窗口」 | 系统缺少 WebView2 运行时 | 安装 [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/) 后重试 |

## 它把数据存在哪

- 缓存（WCL 响应、地图图片、技能名称）：`%LOCALAPPDATA%\WCLAnalyzer\cache`
- 人机验证后的会话 cookie：`%LOCALAPPDATA%\WCLAnalyzer\cache\wcl_cookies.json`
- 删掉这个目录不影响使用，只是下次分析会重新拉取、重新验证。
- 没有账号、没有数据库、不上传任何东西；所有分析都在本机完成。

## 需要联网

程序本身要访问 `warcraftlogs.com`（拉日志）、`wowhead.com`（技能名/图标）以及
页面用的 ECharts CDN，所以**必须联网**。大陆网络访问 WCL 可能较慢，首次分析
一份大秘境日志（含回放）大约要几十秒，之后有缓存会快很多。

## 大秘境「导出 MDT」

导出路线所需的 MDT 地下城与怪物数据已随程序内置，无需另外安装 MDT 插件或设置路径。
复制导入字符串后，在游戏内 MDT 选择对应地下城，打开路线菜单并选择「导入」即可。
