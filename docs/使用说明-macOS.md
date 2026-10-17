# 分享给朋友（macOS 版）

这一页是写给**拿到程序的人**的，可以直接整段复制给对方。

## 先选对芯片

点左上角苹果菜单 →「关于本机」，看「芯片」一栏：

| 芯片 | 下载 |
| --- | --- |
| Apple M1 / M2 / M3 / M4… | `WCL-Analyzer-macos-arm64.zip` |
| Intel Core i5 / i7 / i9… | `WCL-Analyzer-macos-x86_64.zip` |

Intel 版在 Apple 芯片上可以通过 Rosetta 2 运行；反过来不行，`arm64` 版在 Intel Mac 上会报
`Bad CPU type in executable`。

下面的命令以 Apple 芯片（`arm64`）为例；Intel Mac 把文件名换成 `WCL-Analyzer-macos-x86_64` 即可。

## 三步用起来

1. 打开 <https://github.com/LiQunXia/wcl-analyzer/releases/latest>，下载对应芯片的 zip
   （不用注册 GitHub）。
2. 解压后打开「终端」，进入解压出来的目录运行：

   ```sh
   cd ~/Downloads
   unzip WCL-Analyzer-macos-arm64.zip -d WCL-Analyzer-macos-arm64
   cd WCL-Analyzer-macos-arm64
   chmod +x WCL-Analyzer
   ./WCL-Analyzer
   ```

   如果 macOS 提示“无法打开”或“已损坏”，先清掉下载隔离属性再运行：

   ```sh
   xattr -dr com.apple.quarantine WCL-Analyzer
   ./WCL-Analyzer
   ```

3. 浏览器大约 1 秒后自动打开 <http://127.0.0.1:8000>
   （8000 被别的程序占用时会自动换端口，以终端里打印的地址为准）。
   把 Warcraft Logs 的报告链接粘进输入框，选择战斗即可。

**不要关掉终端窗口。** 用完之后在终端按 `Ctrl+C`，或直接关闭这个窗口，程序就退出了。

## 如果弹出「WCL 人机验证」窗口

Warcraft Logs 有时会要求确认访问者不是机器人。这时程序会弹出一个小窗口，
**由你自己点击页面上的「I am a human and not a bot」**（程序不会替你点）。
验证通过后窗口会自动关闭，分析继续进行。

macOS 版用的是系统自带的 WKWebView，不需要额外安装运行时。验证结果会保存在
`~/WCLAnalyzer/cache/wcl_cookies.json`，一段时间内不需要重复验证；想重新验证时
删掉这个文件即可。

## 第一次运行时可能遇到

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| “无法打开，因为 Apple 无法检查其是否包含恶意软件” | 程序没有 Apple 开发者签名和公证 | 打开“系统设置 → 隐私与安全性”，在“安全性”一栏点“仍要打开”；或在终端执行 `xattr -dr com.apple.quarantine WCL-Analyzer`。macOS 15 起右键“打开”已不能绕过 Gatekeeper |
| “已损坏，无法打开。你应该将它移到废纸篓” | 下载的 zip 带隔离属性 | 在终端执行 `xattr -dr com.apple.quarantine WCL-Analyzer` 后重试 |
| `Bad CPU type in executable` | 下错了芯片架构 | 按上面的表格换成另一个 zip |
| 双击后一闪而过 | 这是命令行程序，双击会打开终端；出错时窗口可能很快关闭 | 按上面的终端命令运行，就能看到完整报错 |
| 浏览器没自动打开 | 默认浏览器关联异常 | 手动打开终端里打印的地址 |
| 提示端口被占用 | 8000 已被别的程序用了 | 程序会自动换端口，按终端里打印的地址打开 |
| 第一次启动要等几秒 | 单文件版要先解压到临时目录 | 正常现象，之后会快一些 |
| 提示「无法打开验证窗口」 | 系统 WebKit 异常，或被安全软件拦截 | 从终端运行看具体报错，升级 macOS 后重试 |
| 杀毒软件报警 | PyInstaller 打包的程序常被误报 | 加入信任白名单；不放心可以从源码自行构建 |

## 它把数据存在哪

- 缓存（WCL 响应、地图图片、技能名称）：`~/WCLAnalyzer/cache`
- 人机验证后的会话 cookie：`~/WCLAnalyzer/cache/wcl_cookies.json`
- 验证窗口的 WebView 数据：`~/WCLAnalyzer/webview`

想换位置可以设置 `WCL_DATA_DIR`：

```sh
WCL_DATA_DIR="$HOME/Library/Application Support/WCLAnalyzer" ./WCL-Analyzer
```

删掉 `~/WCLAnalyzer` 不影响使用，只是下次分析会重新拉取、重新验证。

## 导出 MDT 路线

macOS 版会默认在下面几个位置找 Mythic Dungeon Tools 插件：

```text
/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools
~/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools
~/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools
~/Games/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools
```

插件装在别处时，启动前用 `WCL_MDT_PATH` 指过去：

```sh
WCL_MDT_PATH="/Volumes/Games/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools" ./WCL-Analyzer
```
