from __future__ import annotations

import argparse
import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from backend.app.main import app


APP_TITLE = "产线周排班"
DEFAULT_DESKTOP_PORT = 61375


def find_available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_desktop_port(requested_port: int) -> int:
    return requested_port or DEFAULT_DESKTOP_PORT


def wait_until_ready(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
        time.sleep(0.1)
    raise RuntimeError("本地排班服务启动超时") from last_error


def make_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)


def run_headless(port: int) -> None:
    make_server(port).run()


def run_desktop(port: int) -> None:
    try:
        import webview
    except ImportError as error:
        raise RuntimeError("桌面窗口组件未安装，请先安装 requirements-desktop.txt") from error

    server = make_server(port)
    thread = threading.Thread(target=server.run, name="scheduler-server", daemon=True)
    thread.start()
    # CloudBase 默认放行 localhost 作为本地开发来源。服务仍只绑定
    # 127.0.0.1，既不会暴露到局域网，也避免要求用户购买自定义域名能力。
    url = f"http://localhost:{port}"
    wait_until_ready(url)

    webview.create_window(
        APP_TITLE,
        url=url,
        width=1380,
        height=900,
        min_size=(1024, 680),
        text_select=True,
        confirm_close=False,
    )
    try:
        webview.start(debug=False)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--headless", action="store_true", help="仅启动服务，用于测试")
    parser.add_argument("--port", type=int, default=0, help="本地服务端口")
    args = parser.parse_args()
    port = args.port if args.headless else resolve_desktop_port(args.port)
    if args.headless and not port:
        port = find_available_port()
    if args.headless:
        run_headless(port)
    else:
        run_desktop(port)


if __name__ == "__main__":
    main()
