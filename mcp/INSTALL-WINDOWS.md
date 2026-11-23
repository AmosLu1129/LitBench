# LitBench MCP — Windows 安装说明

这是 **MIT 薄层**，只通过 HTTP 访问 LitBench API，不包含服务端代码。

## 1. 安装

需要 Python 3.11+（推荐 3.12）。在解压目录执行：

```powershell
python -m pip install .\litbench_mcp-0.1.0-py3-none-any.whl
```

安装后提供 `litbench-mcp` 命令；也可以用 `python -m litbench_mcp`。

## 2. 确认 LitBench API 在跑

Web 开发版默认 `http://127.0.0.1:8000`：

```powershell
curl.exe http://127.0.0.1:8000/api/ready
```

如果 API 打开了 `AUTH_ENABLED=1`，见下面「内部令牌」。

## 3. 接入 MCP 客户端（stdio，推荐）

Claude Desktop / DSH / Cursor 的 MCP 配置里加：

```json
{
  "mcpServers": {
    "litbench": {
      "command": "python",
      "args": ["-m", "litbench_mcp", "serve", "--api", "http://127.0.0.1:8000"]
    }
  }
}
```

`command` 建议写 Python 的绝对路径（Windows 上通常是
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`）。stdio 模式不占端口。

## 4. HTTP 传输（可选）

```powershell
# 签发一个只读 token（输出只显示一次）
litbench-mcp token issue --scope agent-read --label dsh

litbench-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

客户端连接 `http://127.0.0.1:8765/mcp`，带 `Authorization: Bearer <token>`。
写侧工具需要再加 `agent-propose` / `agent-task` scope。

## 5. 认证打开时的内部令牌

API 设了 `INTERNAL_API_TOKEN` 时，MCP 侧需要同一个值：

```powershell
$env:LITBENCH_MCP_API_TOKEN = "<同一个随机串>"
```

## 6. 常用环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LITBENCH_MCP_API` | `http://127.0.0.1:8000` | API 地址 |
| `LITBENCH_MCP_STATE_DIR` | `.data/mcp` | token / audit 目录 |
| `LITBENCH_MCP_API_TOKEN` | 空 | 服务端内部令牌 |
| `LITBENCH_MCP_RATE_LIMIT` | `120` | 每 60 秒调用上限 |

完整工具面、错误码和 scope 见包内 `README.md`。
