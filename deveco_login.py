#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevEco Code 华为账号登录半自动脚本

逆向自：packages/opencode/src/plugin/deveco.ts

流程：
1. 在本地 127.0.0.1 启动一个临时 HTTP server（默认端口 10101），监听 /callback。
2. 生成一个 clientSecret（UUID 去掉横线）。
3. 打开浏览器访问：
   https://cn.devecostudio.huawei.com/console/DevEcoIDE/apply?port=<port>&appid=1008&code=<clientSecret>
4. 用户在浏览器中完成华为账号登录/授权。
5. 授权完成后，浏览器回调本地 server：
   POST/GET /callback?code=<clientSecret>&tempToken=<...>&siteId=1[&quit=...]
6. 脚本用 tempToken 换取 jwtToken：
   GET https://cn.devecostudio.huawei.com/authrouter/auth/api/temptoken/check
       ?tempToken=<...>&site=CN&version=1.0.0&appid=1008
7. 用 jwtToken 获取用户 token 信息：
   GET https://cn.devecostudio.huawei.com/authrouter/auth/api/jwToken/check
       Headers: jwtToken=<jwtToken>, refresh=false
8. 解析 jwtToken payload，输出 userId、userName、accessToken、refreshToken 等。

依赖：
    pip install requests

用法：
    python scripts/deveco_login.py
    python scripts/deveco_login.py --port 10101 --no-browser
"""

import argparse
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from typing import Optional

# 优先用 requests；若未安装则回退标准库 urllib
import requests

# ---------------------------------------------------------------------------
# 常量配置（与 packages/opencode/src/plugin/deveco.ts 中 DEFAULT_CONFIG 对应）
# ---------------------------------------------------------------------------

BASE_URL = "https://cn.devecostudio.huawei.com"
AUTH_URL = "console/DevEcoIDE/apply"
TEMP_TOKEN_CHECK_URL = "authrouter/auth/api/temptoken/check"
JWT_TOKEN_CHECK_URL = "authrouter/auth/api/jwToken/check"
SUCCESS_REDIRECT_URL = "console/DevEcoCode/loginSuccess"
FAILED_REDIRECT_URL = "console/DevEcoCode/loginFailed"
APP_ID = "1008"
DEFAULT_PORT = 10101
TIMEOUT_MS = 600_000  # 10 分钟

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "accept-language": "zh-CN",
}


# ---------------------------------------------------------------------------
# 回调服务器
# ---------------------------------------------------------------------------

VERBOSE = False
callback_event = threading.Event()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    expected_code: Optional[str] = None
    result: Optional[dict] = None

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, extra_headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _collect_params(self, body: str = "") -> dict:
        parsed = urllib.parse.urlparse(self.path)
        if body and body.strip():
            src = urllib.parse.parse_qs(body, keep_blank_values=True)
        else:
            src = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in src.items()}

    def _handle(self, body: str = "") -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = self._collect_params(body)
        code = params.get("code", "")
        temp_token = params.get("tempToken", "")
        site_id = params.get("siteId", "")
        quit_flag = params.get("quit", "")

        if VERBOSE:
            print(f"\n[verbose] 收到请求 {self.command} {self.path}")
            print(f"[verbose] headers: {dict(self.headers)}")
            print(f"[verbose] body: {body[:500]}")
            print(f"[verbose] parsed params: {params}")
            print(f"[verbose] expected_code: {CallbackHandler.expected_code}")
            print(f"[verbose] code_match: {code == CallbackHandler.expected_code}")

        # code 不匹配说明不是本次登录回调，忽略并保持等待
        if code != CallbackHandler.expected_code:
            msg = b"<h1>Waiting for authorization...</h1><p>Code mismatch, still waiting.</p>"
            self._send(200, msg)
            return

        CallbackHandler.result = {
            "code": code,
            "tempToken": temp_token,
            "siteId": site_id,
            "quit": quit_flag,
        }
        callback_event.set()

        # 根据是否取消/失败决定重定向页面
        if quit_flag in ("true", "access_denied") or not temp_token or site_id != "1":
            location = f"{BASE_URL}/{FAILED_REDIRECT_URL}"
        else:
            location = f"{BASE_URL}/{SUCCESS_REDIRECT_URL}"

        self._send(
            302,
            b"",
            extra_headers={"Location": location},
        )

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        self._handle(body)


def start_callback_server(port: int, expected_code: str) -> tuple[http.server.HTTPServer, int]:
    """启动本地回调服务器，若端口占用则尝试附近端口。"""
    CallbackHandler.expected_code = expected_code
    CallbackHandler.result = None
    callback_event.clear()

    ports_to_try = [port, 34567, 34568, 34569, 34570]
    for p in ports_to_try:
        try:
            server = http.server.HTTPServer(("127.0.0.1", p), CallbackHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, p
        except OSError as e:
            if e.errno != 98:  # EADDRINUSE
                raise
    raise RuntimeError(f"所有端口均被占用: {ports_to_try}")


def set_verbose(enabled: bool) -> None:
    global VERBOSE
    VERBOSE = enabled


def wait_for_callback(server: http.server.HTTPServer, timeout_ms: int) -> dict:
    signaled = callback_event.wait(timeout=timeout_ms / 1000)
    server.shutdown()
    if not signaled or CallbackHandler.result is None:
        raise TimeoutError("等待浏览器回调超时")
    return CallbackHandler.result


# ---------------------------------------------------------------------------
# HTTP 请求辅助
# ---------------------------------------------------------------------------

def http_get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: int = 30) -> dict:
    """使用 requests 发起 GET 请求并返回原始响应对象字典。"""
    h = {**DEFAULT_HEADERS, **(headers or {})}
    resp = requests.get(url, params=params, headers=h, timeout=timeout)
    return {
        "status_code": resp.status_code,
        "text": resp.text,
        "json": safe_json(resp),
        "headers": dict(resp.headers),
    }


def self_test_callback(port: int, expected_code: str) -> bool:
    """启动 server 后先自测回调是否可达。"""
    test_url = f"http://127.0.0.1:{port}/callback?code={expected_code}&tempToken=test&siteId=1"
    print(f"[*] 本地回调自测: {test_url}")
    try:
        resp = http_get(test_url, timeout=5)
        print(f"[+] 自测响应 HTTP {resp['status_code']}")
        if resp["status_code"] == 302:
            print(f"[+] 本地回调服务正常，会重定向到: {resp.get('headers', {}).get('Location', 'success page')}")
            return True
        return True
    except Exception as e:
        print(f"[-] 自测失败: {e}")
        print("    可能原因: 防火墙/安全软件拦截了 localhost 连接，或端口绑定异常。")
        return False


def safe_json(resp) -> Optional[dict]:
    try:
        return resp.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JWT 解析
# ---------------------------------------------------------------------------

def parse_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("jwtToken 格式不正确")
    payload_b64 = parts[1]
    # URL-safe base64 补全
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# 主登录流程
# ---------------------------------------------------------------------------

def generate_client_secret() -> str:
    return uuid.uuid4().hex


def open_login_page(port: int, client_secret: str, no_browser: bool) -> None:
    login_url = f"{BASE_URL}/{AUTH_URL}?port={port}&appid={APP_ID}&code={client_secret}"
    print(f"[*] 请在浏览器中完成华为账号授权：\n    {login_url}\n")
    if not no_browser:
        webbrowser.open(login_url)


def exchange_temp_token(temp_token: str) -> str:
    url = f"{BASE_URL}/{TEMP_TOKEN_CHECK_URL}"
    params = {
        "tempToken": temp_token,
        "site": "CN",
        "version": "1.0.0",
        "appid": APP_ID,
    }
    print(f"[*] GET {url}")
    print(f"    params={params}")
    resp = http_get(url, params=params)
    if resp["status_code"] != 200:
        raise RuntimeError(f"换取 jwtToken 失败，HTTP {resp['status_code']}: {resp['text']}")

    jwt_token = resp["text"].strip()
    if jwt_token.count(".") != 2:
        raise ValueError(f"返回的 jwtToken 格式不正确: {jwt_token[:80]}")
    return jwt_token


def check_jwt_token(jwt_token: str) -> dict:
    url = f"{BASE_URL}/{JWT_TOKEN_CHECK_URL}"
    headers = {
        "refresh": "false",
        "jwtToken": jwt_token,
    }
    print(f"[*] GET {url}")
    print(f"    headers={{refresh: false, jwtToken: {jwt_token[:20]}...}}")
    resp = http_get(url, headers=headers)
    if resp["status_code"] != 200:
        raise RuntimeError(f"校验 jwtToken 失败，HTTP {resp['status_code']}: {resp['text']}")

    data = resp["json"]
    if data is None:
        raise ValueError(f"校验 jwtToken 返回非 JSON: {resp['text']}")
    if not data.get("status") or not data.get("userInfo"):
        raise ValueError(f"jwtToken 校验未通过: {data}")
    return data


def run_login(port: int, no_browser: bool, timeout_ms: int, verbose: bool = False) -> int:
    set_verbose(verbose)
    client_secret = generate_client_secret()
    print("[*] DevEco Code 华为账号登录")
    print(f"    baseUrl:      {BASE_URL}")
    print(f"    clientSecret: {client_secret}")

    server, actual_port = start_callback_server(port, client_secret)
    print(f"[+] 本地回调服务器已启动: http://127.0.0.1:{actual_port}/callback")

    # 先自测一下本地回调是否可达
    self_test_callback(actual_port, client_secret)
    # 自测会设置 CallbackHandler.result，重置一下
    CallbackHandler.result = None

    try:
        open_login_page(actual_port, client_secret, no_browser)
        print(f"[*] 等待浏览器回调（超时 {timeout_ms // 1000}s）...")
        callback = wait_for_callback(server, timeout_ms)

        if callback.get("quit") in ("true", "access_denied"):
            print("[-] 用户在浏览器中取消了授权")
            return 1

        site_id = callback.get("siteId", "")
        if site_id != "1":
            print(f"[-] 不支持的 region，siteId={site_id}（目前只支持 siteId=1 中国区）")
            return 1

        temp_token = callback.get("tempToken", "").split("&")[0]
        if not temp_token:
            print(f"[-] 回调缺少 tempToken: {callback}")
            return 1

        print(f"[+] 收到回调，tempToken={temp_token[:24]}...")

        jwt_token = exchange_temp_token(temp_token)
        print(f"[+] 获得 jwtToken: {jwt_token[:64]}...")

        check_result = check_jwt_token(jwt_token)
        user_info = check_result["userInfo"]
        jwt_payload = parse_jwt(jwt_token)

        result = {
            "success": True,
            "jwtToken": jwt_token,
            "jwtPayload": jwt_payload,
            "userInfo": user_info,
        }

        print("\n" + "=" * 60)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("=" * 60)
        print(f"\n[+] 登录成功")
        print(f"    userId:       {jwt_payload.get('userId', '')}")
        print(f"    userName:     {jwt_payload.get('userName', '')}")
        print(f"    accessToken:  {user_info.get('accessToken', '')[:40]}...")
        print(f"    refreshToken: {user_info.get('refreshToken', '')[:40]}...")
        print(f"    nationalCode: {user_info.get('nationalCode', '')}")
        print(f"    realName:     {user_info.get('realName', '')}")
        return 0

    except TimeoutError:
        print("[-] 等待浏览器回调超时，请重试")
        return 1
    except Exception as e:
        print(f"[-] 登录失败: {e}")
        return 1
    finally:
        try:
            server.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="DevEco Code 华为账号登录半自动脚本（打开浏览器授权并获取 token）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/deveco_login.py
  python scripts/deveco_login.py --port 10101 --no-browser
  python scripts/deveco_login.py --timeout 300000
""",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="本地回调服务器端口（默认 10101）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印登录 URL")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_MS, help="等待回调超时毫秒数（默认 600000）")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印所有回调请求详情，用于排查问题")
    args = parser.parse_args(argv)

    return run_login(args.port, args.no_browser, args.timeout, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
