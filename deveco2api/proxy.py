# -*- coding: utf-8 -*-
"""OpenAI 兼容 API 代理，转发至 DevEco MaaS。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import refresh_access_token_sync
from .config import Config, save_config
from .id import chat_id, message_id, session_id
from .logger import setup_logger

logger = setup_logger("deveco2api.proxy")


bearer_scheme = HTTPBearer(auto_error=False)


def _session_chat_id_map() -> dict[str, str]:
    # 单进程内按 session_id 缓存 Chat-Id
    if not hasattr(_session_chat_id_map, "cache"):
        _session_chat_id_map.cache = {}
    return _session_chat_id_map.cache


def _get_chat_id(session_id_value: str) -> str:
    cache = _session_chat_id_map()
    if session_id_value not in cache:
        cache[session_id_value] = chat_id()
    return cache[session_id_value]


def _verify_api_key(
    config: Config, credentials: Optional[HTTPAuthorizationCredentials]
) -> None:
    expected = config.server.api_key
    if not expected:
        return
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _build_deveco_headers(config: Config, session_id_value: str, user_msg_id: str) -> dict[str, str]:
    access_token = config.deveco.auth.access_token
    chat_id_value = _get_chat_id(session_id_value)
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Chat-Id": chat_id_value,
        "Session-Id": session_id_value,
        "x-deveco-client": config.deveco.client,
        "x-deveco-project": config.deveco.project,
        "x-deveco-request": user_msg_id,
        "x-deveco-session": session_id_value,
        "User-Agent": config.deveco.user_agent,
        "lang": "en",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Connection": "keep-alive",
    }
    return headers


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留原始 messages，仅确保 content 为字符串。"""
    normalized: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # 简单将多模态内容拼接为文本；实际可按需扩展
            parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        parts.append(f"[image: {part.get('image_url', {}).get('url', '')}]")
            content = "\n".join(parts)
        normalized.append({"role": role, "content": content})
    return normalized


def _build_deveco_body(config: Config, request_body: dict[str, Any]) -> dict[str, Any]:
    model = request_body.get("model", config.deveco.model)
    messages = _normalize_messages(request_body.get("messages", []))
    stream = request_body.get("stream", False)
    max_tokens = request_body.get("max_tokens", 32000)

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}

    # 透传工具调用相关参数
    if "tools" in request_body:
        body["tools"] = request_body["tools"]
    if "tool_choice" in request_body:
        tc = request_body["tool_choice"]
        # DevEco 后端只接受字符串枚举，OpenAI 对象形式统一映射为 required
        if isinstance(tc, dict) and tc.get("type") == "function":
            body["tool_choice"] = "required"
        elif tc in ("none", "auto", "required"):
            body["tool_choice"] = tc
        else:
            body["tool_choice"] = "auto"

    # 透传其他常见参数
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "stop", "seed"):
        if key in request_body:
            body[key] = request_body[key]
    return body


def _create_app(config: Config, config_path: str = "config.toml") -> FastAPI:
    app = FastAPI(title="DevEco2API", version="0.1.0")

    # 复用异步 httpx 客户端，保持连接池
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    @app.on_event("shutdown")
    async def _close_client():
        await client.aclose()

    @app.get("/v1/models")
    async def list_models(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
        _verify_api_key(config, credentials)
        url = f"{config.deveco.base_url.rstrip('/')}/codeGenie/modelConfig"
        params = {"localVersion": "0", "pluginVersion": "CLI.0.1.0"}
        headers = {
            "Authorization": f"Bearer {config.deveco.auth.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "opencode/0.1.0",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Connection": "keep-alive",
        }
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("获取模型列表失败: %s", e)
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

        models: list[dict[str, Any]] = []
        body = data.get("body", {})
        for group in body.get("inner_models", []):
            for cfg in group.get("model_configs", []):
                model_id = cfg.get("model_id")
                if model_id:
                    models.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": group.get("group_name", "deveco"),
                        }
                    )
        return {"object": "list", "data": models}

    async def _try_refresh_token() -> bool:
        """使用 jwt_token 刷新 access_token，成功则更新内存与配置文件。"""
        jwt_token = config.deveco.auth.jwt_token
        if not jwt_token:
            return False
        try:
            data = await asyncio.to_thread(refresh_access_token_sync, base_url, jwt_token)
            user_info = data["userInfo"]
            config.deveco.auth.access_token = user_info.get("accessToken", "")
            config.deveco.auth.refresh_token = user_info.get("refreshToken", "")
            config.deveco.auth.user_id = user_info.get("userId", "")
            config.deveco.auth.user_name = user_info.get("name", "")
            save_config(config, config_path)
            logger.info("access_token 运行时刷新成功")
            return True
        except Exception as e:
            logger.error("运行时刷新 access_token 失败: %s", e)
            return False

    async def _call_upstream(stream: bool, url: str, headers: dict[str, str], body: dict[str, Any], session_id_value: str):
        if stream:
            upstream = await client.post(url, headers=headers, json=body)
            upstream.raise_for_status()
            return StreamingResponse(
                _stream_response(upstream, session_id_value),
                media_type="text/event-stream",
            )
        else:
            upstream = await client.post(url, headers=headers, json=body)
            upstream.raise_for_status()
            return JSONResponse(content=upstream.json())

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        _verify_api_key(config, credentials)
        try:
            request_body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

        stream = request_body.get("stream", False)
        session_id_value = request_body.get("session_id") or session_id()
        user_msg_id = message_id()

        deveco_body = _build_deveco_body(config, request_body)
        base_url = config.deveco.base_url.rstrip("/")
        path = "/sse/codeGenie/maas/v2"
        if stream:
            url = f"{base_url}{path}/chat/completions"
        else:
            url = f"{base_url}{path}/no-stream/chat/completions"

        logger.info("POST %s model=%s stream=%s", url, deveco_body.get("model"), stream)

        deveco_headers = _build_deveco_headers(config, session_id_value, user_msg_id)
        logger.debug(
            "headers=%s",
            {
                k: (v[:20] + "..." if k.lower() in ("authorization", "jwttoken") else v)
                for k, v in deveco_headers.items()
            },
        )

        try:
            return await _call_upstream(stream, url, deveco_headers, deveco_body, session_id_value)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401 and await _try_refresh_token():
                deveco_headers = _build_deveco_headers(config, session_id_value, user_msg_id)
                try:
                    return await _call_upstream(stream, url, deveco_headers, deveco_body, session_id_value)
                except httpx.HTTPStatusError as e2:
                    text2 = e2.response.text
                    logger.error("刷新后上游请求失败 HTTP %s: %s", e2.response.status_code, text2[:500])
                    raise HTTPException(status_code=502, detail=f"Upstream HTTP error: {text2[:500]}")
            text = ""
            try:
                text = e.response.text
            except Exception:
                pass
            logger.error("上游请求失败 HTTP %s: %s", getattr(e.response, "status_code", "?"), text[:500])
            raise HTTPException(status_code=502, detail=f"Upstream HTTP error: {text[:500]}")
        except httpx.HTTPError as e:
            logger.error("上游请求异常: %s", e)
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


async def _stream_response(
    upstream: httpx.Response, session_id_value: str
) -> AsyncGenerator[str, None]:
    try:
        async for line in upstream.aiter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(payload)
                    # 标准化为 OpenAI 格式
                    if "choices" in chunk:
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            if "role" not in delta:
                                delta.setdefault("role", "assistant")
                except json.JSONDecodeError:
                    pass
                yield f"data: {payload}\n\n"
            # 跳过 id: / event: 等非 data: 的 SSE 字段，保持 OpenAI 标准格式
    finally:
        await upstream.aclose()


def create_app(config: Config, config_path: str = "config.toml") -> FastAPI:
    return _create_app(config, config_path)
