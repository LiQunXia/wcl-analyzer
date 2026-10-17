"""桌面启动入口：挑一个空闲端口，在本机起服务，然后自动打开浏览器。

双击打包好的 exe 时运行的就是这个文件（见 wcl.spec）。从源码运行时效果与
``./start`` 相同，只是会顺手打开浏览器，并且端口被占用时自动换一个。
"""

import argparse
import socket
import sys
import threading
import webbrowser

# Windows 的英文控制台是 cp1252，直接打印中文会抛 UnicodeEncodeError。
# 换成 UTF-8 并对无法编码的字符做替换，至少不会让程序崩在启动的第一行。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 打包后的 exe 会以 ``--verify`` 重新启动自己，只为弹出人机验证窗口；
# 这个分支必须在 import server 之前处理，不能把服务再起一遍。
if "--verify" in sys.argv:
    import wcl_verify

    _index = sys.argv.index("--verify")
    raise SystemExit(wcl_verify.main(sys.argv[_index + 1:]))

import server  # noqa: E402  (must come after the stdout fix)


def free_port(preferred=8000):
    """Return ``preferred`` if it is free, otherwise an arbitrary free port."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise SystemExit("找不到可用端口，请关闭其他占用 8000 端口的程序后重试")


def open_later(url, delay=1.0):
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main(argv=None):
    parser = argparse.ArgumentParser(description="WCL 治疗作业（本地桌面版）")
    parser.add_argument("--port", type=int, default=8000, help="首选端口，被占用时自动更换")
    parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    args = parser.parse_args(argv)

    port = free_port(args.port)
    httpd = server.serve("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}"

    print()
    print(f"  治疗作业 已启动：{url}", flush=True)
    print("  浏览器会自动打开。用完后关闭这个窗口即可退出。", flush=True)
    print(flush=True)

    if not args.no_browser:
        open_later(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        # 双击运行时没有终端保留错误信息，停一下让人能看清。
        if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
            try:
                input("\n启动失败，按回车键退出……")
            except (EOFError, KeyboardInterrupt):
                pass
        raise SystemExit(1)
