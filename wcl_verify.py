"""由用户亲自通过 WCL 的人机验证，再把会话 cookie 交给父进程。

WCL 在识别出非浏览器请求时，会在网页上显示一个“确认你是人类”的按钮。算法无法
替用户做这个决定，所以这里的做法是：

1. 父进程（``server.py``）遇到验证页时，用 ``--verify <日志链接>`` 启动一个子进程；
2. 子进程弹出系统自带的 WebView（Windows 是 WebView2，macOS 是 WKWebView），
   加载 WCL 自己的页面；
3. 用户自己在窗口里点击“我是人类”。验证页消失后，子进程读取浏览器里的 cookie，
   通过 stdout 上的一行 ``__WCL_VERIFY__ {json}`` 交回父进程；
4. 父进程把 cookie 注入 ``curl_cffi`` 的会话，之后的请求全部自动完成。

子进程协议只认 ``__WCL_VERIFY__`` 前缀那一行，stdout 上的其它输出（浏览器、
原生库的日志）一律忽略，避免污染结果。

本模块在导入时不加载 ``webview``，所以没安装 pywebview 时服务仍能启动，只有在
真的需要人工验证时才会提示安装。
"""

import json
import os
import sys
import threading
import time
from http.cookies import BaseCookie, Morsel
from pathlib import Path

from localization import translate

VERIFY_FLAG = "--verify"
RESULT_PREFIX = "__WCL_VERIFY__ "

# 挑战可能把 token 和 User-Agent 绑定，父进程的 curl_cffi 会话使用同一个字符串。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

WCL_HOST = "warcraftlogs.com"
TIMEOUT_SEC = 300
PROBE_INTERVAL_SEC = 0.5
SETTLE_SEC = 1.0

# 验证完成后页面上既没有 human-challenge 表单，也没有验证页的标题。
_DONE_JS = r"""
(() => {
  try {
    if (document.readyState !== 'complete') return false;
    if (document.querySelector('#challenge-form, #cf-challenge-running, form[action*="human-challenge"]')) return false;
    const body = document.body;
    const text = body ? body.innerText : '';
    if (!text || text.length < 80) return false;
    if (/One Quick Check|Human Verification|人机验证|确认您是真人|Verify you are human|Checking your browser/i.test(text)) return false;
    return true;
  } catch (e) {
    return false;
  }
})()
"""


def _field(cookie, name, default=None):
    """pywebview 不同后端返回的 cookie 可能是 Morsel、dict 或对象。"""
    if isinstance(cookie, dict):
        return cookie.get(name, default)
    return getattr(cookie, name, default)


def _morsels(cookie):
    """pywebview 返回的是 ``http.cookies.SimpleCookie``，每个对象含一个 Morsel。"""
    if isinstance(cookie, BaseCookie):
        return list(cookie.values())
    if isinstance(cookie, Morsel):
        return [cookie]
    return [cookie]


def _cookie_name(cookie):
    if isinstance(cookie, Morsel):
        return cookie.key
    return _field(cookie, "name")


def _cookie_value(cookie):
    if isinstance(cookie, Morsel):
        return cookie.value
    return _field(cookie, "value")


def wcl_cookies(cookies):
    """只保留 warcraftlogs.com 的 cookie，并统一成可 JSON 序列化的结构。"""
    seen = {}
    for cookie in cookies or []:
        for morsel in _morsels(cookie):
            domain = str(_field(morsel, "domain", "") or "")
            if WCL_HOST not in domain:
                continue
            name = str(_cookie_name(morsel) or "")
            if not name:
                continue
            path = str(_field(morsel, "path", "") or "/")
            seen[(name, domain, path)] = {
                "name": name,
                "value": str(_cookie_value(morsel) or ""),
                "domain": domain,
                "path": path,
                "secure": bool(_field(morsel, "secure", False)),
            }
    return list(seen.values())


def parse_result(stdout):
    """从子进程输出里取出最后一行结果，没有则返回 None。"""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                return None
    return None


def _emit(payload):
    sys.stdout.write(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _storage_path():
    """持久化 WebView 的 cookie，用户在一段时间内不必重复验证。"""
    env = os.environ.get("WCL_WEBVIEW_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return (Path(base) if base else Path.home()) / "WCLAnalyzer" / "webview"


def _close(window):
    try:
        if window is not None:
            window.destroy()
    except Exception:
        pass


def run(url, timeout=TIMEOUT_SEC):
    """打开验证窗口，返回 ``{"cookies": [...]}`` 或 ``{"error": "..."}``。"""
    try:
        import webview
    except ImportError:
        return {"error": translate("未安装 pywebview，无法弹出验证窗口。请先执行 pip install -r requirements.txt",
                                   "pywebview is not installed. Run pip install -r requirements.txt to open the verification window.")}

    # The WebView watcher may run on another thread, so capture request-local messages here.
    closed_message = translate("验证窗口已关闭，未完成验证", "The verification window was closed before verification completed")
    timeout_message = translate("验证超时，请重试", "Verification timed out. Please try again")
    webview2_message = translate("系统缺少 WebView2 运行时，无法加载 WCL 页面。请安装 Microsoft Edge WebView2 运行时后重试。",
                                 "The WebView2 runtime is missing. Install the Microsoft Edge WebView2 runtime and try again.")
    result = {}
    closed = threading.Event()
    window = None

    def watch():
        renderer = getattr(webview, "renderer", None)
        if renderer == "mshtml":
            result["error"] = webview2_message
            _close(window)
            return
        deadline = time.monotonic() + timeout
        while not closed.is_set() and time.monotonic() < deadline:
            time.sleep(PROBE_INTERVAL_SEC)
            if closed.is_set():
                break
            try:
                if not window.evaluate_js(_DONE_JS):
                    continue
                # 页面刚完成跳转时 cookie 可能还没写完，稍等一下再读。
                time.sleep(SETTLE_SEC)
                cookies = wcl_cookies(window.get_cookies())
                if not cookies:
                    continue
                result["cookies"] = cookies
                result["url"] = window.get_current_url() or url
                _close(window)
                return
            except Exception:
                continue
        if "cookies" not in result:
            result["error"] = closed_message if closed.is_set() else timeout_message
        _close(window)

    try:
        window = webview.create_window(
            translate("WCL 人机验证 —— 请点击“我是人类”", "WCL verification — please click ‘I am a human’"),
            url,
            width=980,
            height=760,
            text_select=True,
        )
        if window is None:
            return {"error": translate("无法创建验证窗口", "Unable to create the verification window")}
        window.events.closed += lambda *_: closed.set()
        storage = _storage_path()
        storage.mkdir(parents=True, exist_ok=True)
        # watch 交给 start 启动（GUI 起来之后再轮询），避免在窗口加载前调用
        # get_cookies/evaluate_js 时长时间等待 loaded 事件。
        webview.start(
            watch,
            private_mode=False,
            storage_path=str(storage),
            user_agent=USER_AGENT,
        )
    except Exception as exc:
        return {"error": translate(f"无法打开验证窗口：{type(exc).__name__}: {exc}",
                                   f"Unable to open the verification window: {type(exc).__name__}: {exc}")}

    if "cookies" in result:
        return result
    return {"error": result.get("error", translate("验证窗口已关闭，未获取到会话", "The verification window closed without a session"))}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == VERIFY_FLAG:
        args = args[1:]
    if not args or not args[0].startswith("http"):
        _emit({"error": translate("缺少有效的 WCL 日志链接", "A valid WCL report link is required")})
        return 2

    result = run(args[0])
    _emit(result)
    return 0 if "cookies" in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
