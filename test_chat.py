#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简单联调脚本：测试本地 OpenAI 兼容 API 的流式与非流式对话。"""

import json
import sys

import requests

# Windows 控制台默认可能为 GBK，强制使用 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="ignore")

BASE_URL = "http://127.0.0.1:10102"
API_KEY = "sk-deveco2api"
MODEL = "GLM-5.1"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def test_models() -> None:
    print("==> GET /v1/models")
    resp = requests.get(f"{BASE_URL}/v1/models", headers=_headers(), timeout=30)
    print(resp.status_code, resp.json())
    assert resp.status_code == 200


def test_non_stream() -> None:
    print("\n==> POST /v1/chat/completions (non-stream)")
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions", headers=_headers(), json=payload, timeout=180
    )
    print(resp.status_code)
    data = resp.json()
    print(json.dumps(data, indent=2, ensure_ascii=False))
    assert resp.status_code == 200
    assert data["choices"][0]["message"]["content"]


def test_stream() -> None:
    print("\n==> POST /v1/chat/completions (stream)")
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers=_headers(),
        json=payload,
        stream=True,
        timeout=60,
    )
    print(resp.status_code)
    content_parts = []
    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode("utf-8", errors="ignore")
        print(text)
        if text.startswith("data: "):
            payload_text = text[6:]
            if payload_text == "[DONE]":
                break
            try:
                chunk = json.loads(payload_text)
                delta = chunk["choices"][0].get("delta", {})
                content_parts.append(delta.get("content", ""))
            except Exception:
                pass
    full = "".join(content_parts)
    print("\n[stream full]", full)
    assert full


def main() -> int:
    try:
        test_models()
        test_non_stream()
        test_stream()
    except Exception as e:
        print(f"\n测试失败: {e}")
        return 1
    print("\n所有测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
