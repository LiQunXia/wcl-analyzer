# 分享给朋友（Windows 双击版）

这一页是写给**拿到程序的人**的，可以直接整段复制给对方。

## 三步用起来

1. 打开 <https://github.com/LiQunXia/wcl-analyzer/releases/latest>，下载 `WCL-Analyzer.exe`
   （约 12 MB，单个文件，不用解压，不用注册 GitHub）。
2. 双击它。会弹出一个黑色窗口，**不要关它**，浏览器大约 1 秒后自动打开
   <http://127.0.0.1:8000>。
3. 把 Warcraft Logs 的报告链接粘进输入框，选择战斗即可。

用完之后，关掉那个黑色窗口就是退出。

## 第一次运行时可能遇到

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 蓝色窗口「Windows 已保护你的电脑」 | exe 没有购买代码签名证书 | 点「更多信息」→「仍要运行」 |
| 杀毒软件报毒 / 直接删掉 | PyInstaller 打包的程序常被误报 | 加入信任白名单，或改用 `--onedir` 版本 |
| 浏览器没自动打开 | 默认浏览器关联异常 | 手动打开 http://127.0.0.1:8000 |
| 提示端口被占用 | 8000 已被别的程序用了 | 程序会自动换端口，按黑色窗口里打印的地址打开 |

## 它把数据存在哪

- 缓存（WCL 响应、地图图片、技能名称）：`%LOCALAPPDATA%\WCLAnalyzer\cache`
- 删掉这个目录不影响使用，只是下次分析会重新拉取。
- 没有账号、没有数据库、不上传任何东西；所有分析都在本机完成。

## 需要联网

程序本身要访问 `warcraftlogs.com`（拉日志）、`wowhead.com`（技能名/图标）以及
页面用的 ECharts CDN，所以**必须联网**。大陆网络访问 WCL 可能较慢，首次分析
一份大秘境日志（含回放）大约要几十秒，之后有缓存会快很多。

## 大秘境「导出 MDT」功能的额外要求

这个功能会读取本机安装的 Mythic Dungeon Tools 插件：

```
World of Warcraft\_retail_\Interface\AddOns\MythicDungeonTools
```

- 装在默认路径、或用暴雪战网安装的，一般能自动找到（程序会查注册表）。
- 装在其它盘符的绿色版，可以设置环境变量指向插件目录：

  ```bat
  set WCL_MDT_PATH=D:\Games\World of Warcraft\_retail_\Interface\AddOns\MythicDungeonTools
  ```

  设置后从同一个命令行窗口启动 exe 即可；也可以直接在「系统属性 → 环境变量」
  里加一条用户变量，以后双击就生效。
- 没装 MDT 也不影响其它功能，只是点「导出 MDT」会提示找不到插件。
