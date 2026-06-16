# DevEco2API

把华为 **DevEco Code** 的云端对话能力封装成 **OpenAI 兼容 API**，供任意支持 OpenAI 格式的客户端使用。

## 快速开始

1. 安装依赖：

```bash
uv sync
```

2. 启动服务：
```bash
uv run main.py
```

## 配置

编辑 `config.toml`：

```toml
[server]
host = "127.0.0.1"
port = 10102
api_key = "sk-deveco2api"

[deveco]
callback_port = 10101
model = "GLM-5.1"
client = "cli"
project = "global"

[logging]
level = "INFO"
```

| 配置项 | 说明 |
|--------|------|
| `server.port` | 本地 OpenAI 兼容 API 端口 |
| `server.api_key` | 访问本地 API 的密钥 |
| `deveco.callback_port` | 浏览器 OAuth 回调监听端口 |
| `deveco.model` | 默认模型 |
| `deveco.client` / `project` | 请求头 `x-deveco-client` / `x-deveco-project` |

## 使用示例

```bash
# 非流式
curl http://127.0.0.1:10102/v1/chat/completions \
  -H "Authorization: Bearer sk-deveco2api" \
  -H "Content-Type: application/json" \
  -d '{"model":"GLM-5.1","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:10102/v1/chat/completions \
  -H "Authorization: Bearer sk-deveco2api" \
  -H "Content-Type: application/json" \
  -d '{"model":"GLM-5.1","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 测试

```bash
uv run test_chat.py
```

## 命令行参数

```bash
uv run main.py --port 10102 --no-browser
uv run main.py --login          # 仅执行登录并保存 token
```
