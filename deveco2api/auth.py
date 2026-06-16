# -*- coding: utf-8 -*-
"""DevEco 华为账号 OAuth 登录与 token 维护。"""

from __future__ import annotations

import base64
import http.server
import json
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .config import Config, DevEcoAuthConfig, save_config
from .logger import setup_logger

logger = setup_logger("deveco2api.auth")


def _default_browser_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN",
    }


def _http_get(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 30,
) -> dict[str, Any]:
    h = {**_default_browser_headers(), **(headers or {})}
    resp = requests.get(url, params=params, headers=h, timeout=timeout)
    return {
        "status_code": resp.status_code,
        "text": resp.text,
        "json": _safe_json(resp),
        "headers": dict(resp.headers),
    }


def _safe_json(resp) -> Optional[dict[str, Any]]:
    try:
        return resp.json()
    except Exception:
        return None


def _parse_jwt(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("jwtToken 格式不正确")
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# 回调服务器
# ---------------------------------------------------------------------------

_callback_event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    expected_code: Optional[str] = None
    result: Optional[dict[str, str]] = None

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, extra_headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _collect_params(self, body: str = "") -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        src = (
            urllib.parse.parse_qs(body, keep_blank_values=True)
            if body and body.strip()
            else urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        )
        return {k: (v[0] if v else "") for k, v in src.items()}

    def _handle(self, body: str = "") -> None:
        params = self._collect_params(body)
        code = params.get("code", "")
        temp_token = params.get("tempToken", "")
        site_id = params.get("siteId", "")
        quit_flag = params.get("quit", "")

        if code != _CallbackHandler.expected_code:
            msg = b"<h1>Waiting for authorization...</h1><p>Code mismatch, still waiting.</p>"
            self._send(200, msg)
            return

        _CallbackHandler.result = {
            "code": code,
            "tempToken": temp_token,
            "siteId": site_id,
            "quit": quit_flag,
        }
        _callback_event.set()

        base_url = "https://cn.devecostudio.huawei.com"
        success_redirect = "console/DevEcoCode/loginSuccess"
        failed_redirect = "console/DevEcoCode/loginFailed"
        if quit_flag in ("true", "access_denied") or not temp_token or site_id != "1":
            location = f"{base_url}/{failed_redirect}"
        else:
            location = f"{base_url}/{success_redirect}"
        self._send(302, b"", extra_headers={"Location": location})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        self._handle(body)


def _start_callback_server(port: int, expected_code: str) -> tuple[http.server.HTTPServer, int]:
    _CallbackHandler.expected_code = expected_code
    _CallbackHandler.result = None
    _callback_event.clear()

    ports_to_try = [port, 34567, 34568, 34569, 34570]
    for p in ports_to_try:
        try:
            server = http.server.HTTPServer(("127.0.0.1", p), _CallbackHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, p
        except OSError as e:
            if e.errno != 98:
                raise
    raise RuntimeError(f"所有端口均被占用: {ports_to_try}")


def _wait_for_callback(server: http.server.HTTPServer, timeout_ms: int) -> dict[str, str]:
    signaled = _callback_event.wait(timeout=timeout_ms / 1000)
    server.shutdown()
    if not signaled or _CallbackHandler.result is None:
        raise TimeoutError("等待浏览器回调超时")
    return _CallbackHandler.result


# ---------------------------------------------------------------------------
# Token 交换
# ---------------------------------------------------------------------------

@dataclass
class LoginResult:
    jwt_token: str
    jwt_payload: dict[str, Any]
    user_info: dict[str, Any]
    access_token: str
    refresh_token: str
    user_id: str
    user_name: str


def _exchange_temp_token(base_url: str, temp_token: str, app_id: str) -> str:
    url = f"{base_url}/authrouter/auth/api/temptoken/check"
    params = {
        "tempToken": temp_token,
        "site": "CN",
        "version": "1.0.0",
        "appid": app_id,
    }
    logger.info("GET %s", url)
    resp = _http_get(url, params=params)
    if resp["status_code"] != 200:
        raise RuntimeError(f"换取 jwtToken 失败，HTTP {resp['status_code']}: {resp['text']}")
    jwt_token = resp["text"].strip()
    if jwt_token.count(".") != 2:
        raise ValueError(f"返回的 jwtToken 格式不正确: {jwt_token[:80]}")
    return jwt_token


def _check_jwt_token(base_url: str, jwt_token: str) -> dict[str, Any]:
    url = f"{base_url}/authrouter/auth/api/jwToken/check"
    headers = {"refresh": "false", "jwtToken": jwt_token}
    logger.info("GET %s", url)
    resp = _http_get(url, headers=headers)
    if resp["status_code"] != 200:
        raise RuntimeError(f"校验 jwtToken 失败，HTTP {resp['status_code']}: {resp['text']}")
    data = resp["json"]
    if data is None:
        raise ValueError(f"校验 jwtToken 返回非 JSON: {resp['text']}")
    if not data.get("status") or not data.get("userInfo"):
        raise ValueError(f"jwtToken 校验未通过: {data}")
    return data


def _refresh_access_token(base_url: str, jwt_token: str) -> dict[str, Any]:
    url = f"{base_url}/authrouter/auth/api/jwToken/check"
    headers = {"refresh": "true", "jwtToken": jwt_token}
    logger.info("GET %s (refresh=true)", url)
    resp = _http_get(url, headers=headers)
    if resp["status_code"] != 200:
        raise RuntimeError(f"刷新 jwtToken 失败，HTTP {resp['status_code']}: {resp['text']}")
    data = resp["json"]
    if data is None or not data.get("status") or not data.get("userInfo"):
        raise ValueError(f"刷新 jwtToken 未通过: {data}")
    return data


def refresh_access_token_sync(base_url: str, jwt_token: str) -> dict[str, Any]:
    """同步刷新 access_token，返回 userInfo。"""
    return _refresh_access_token(base_url, jwt_token)


def login_interactive(config: Config, timeout_ms: int = 600_000, no_browser: bool = False) -> LoginResult:
    """启动本地回调服务器，生成登录 URL，等待浏览器回调并换取 token。"""
    base_url = config.deveco.base_url.rstrip("/")
    port = config.deveco.callback_port
    app_id = config.deveco.app_id

    client_secret = uuid.uuid4().hex
    logger.info("DevEco Code 华为账号登录")
    logger.info("baseUrl: %s", base_url)
    logger.info("clientSecret: %s", client_secret)

    server, actual_port = _start_callback_server(port, client_secret)
    logger.info("本地回调服务器已启动: http://127.0.0.1:%s/callback", actual_port)

    login_url = f"{base_url}/{config.deveco.auth_url}?port={actual_port}&appid={app_id}&code={client_secret}"
    logger.info("请在浏览器中完成华为账号授权：\n    %s", login_url)
    if not no_browser:
        try:
            webbrowser.open(login_url)
        except Exception as e:
            logger.warning("自动打开浏览器失败: %s", e)

    try:
        callback = _wait_for_callback(server, timeout_ms)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass

    if callback.get("quit") in ("true", "access_denied"):
        raise RuntimeError("用户在浏览器中取消了授权")
    if callback.get("siteId", "") != "1":
        raise RuntimeError(f"不支持的 region，siteId={callback.get('siteId')}（目前只支持 siteId=1 中国区）")
    temp_token = callback.get("tempToken", "").split("&")[0]
    if not temp_token:
        raise RuntimeError(f"回调缺少 tempToken: {callback}")

    logger.info("收到回调，tempToken=%s...", temp_token[:24])
    jwt_token = _exchange_temp_token(base_url, temp_token, app_id)
    logger.info("获得 jwtToken: %s...", jwt_token[:64])

    check_result = _check_jwt_token(base_url, jwt_token)
    user_info = check_result["userInfo"]
    jwt_payload = _parse_jwt(jwt_token)

    access_token = user_info.get("accessToken", "")
    refresh_token = user_info.get("refreshToken", "")
    user_id = user_info.get("userId", "") or jwt_payload.get("userId", "")
    user_name = user_info.get("name", "") or jwt_payload.get("userName", "")

    return LoginResult(
        jwt_token=jwt_token,
        jwt_payload=jwt_payload,
        user_info=user_info,
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user_id,
        user_name=user_name,
    )


def save_login_result(config: Config, result: LoginResult, path: str = "config.toml") -> None:
    config.deveco.auth = DevEcoAuthConfig(
        jwt_token=result.jwt_token,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user_id=result.user_id,
        user_name=result.user_name,
    )
    save_config(config, path)
    logger.info("登录信息已保存到 %s", path)


def _test_access_token(base_url: str, access_token: str) -> bool:
    url = f"{base_url}/codeGenie/modelConfig"
    params = {"localVersion": "0", "pluginVersion": "CLI.0.1.0"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "opencode/0.1.0",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        return resp.status_code == 200 and resp.json().get("success") is True
    except Exception:
        return False


def ensure_auth(config: Config, config_path: str = "config.toml") -> str:
    """确保 access_token 有效，必要时刷新或触发登录。"""
    auth = config.deveco.auth
    base_url = config.deveco.base_url.rstrip("/")

    # 如果 access_token 存在且有效，直接复用
    if auth.access_token:
        logger.info("检测现有 access_token 是否有效")
        if _test_access_token(base_url, auth.access_token):
            logger.info("现有 access_token 有效")
            return auth.access_token
        logger.warning("现有 access_token 已失效")

    # 尝试使用 jwt_token 刷新 access_token
    if auth.jwt_token:
        try:
            logger.info("尝试使用现有 jwt_token 刷新 access_token")
            data = _refresh_access_token(base_url, auth.jwt_token)
            user_info = data["userInfo"]
            config.deveco.auth.access_token = user_info.get("accessToken", "")
            config.deveco.auth.refresh_token = user_info.get("refreshToken", "")
            config.deveco.auth.user_id = user_info.get("userId", "")
            config.deveco.auth.user_name = user_info.get("name", "")
            save_config(config, config_path)
            logger.info("access_token 刷新成功")
            return config.deveco.auth.access_token
        except Exception as e:
            logger.warning("刷新失败，将重新登录: %s", e)

    logger.warning("开始华为账号登录流程")
    result = login_interactive(config)
    save_login_result(config, result, config_path)
    return result.access_token
