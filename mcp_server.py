"""MCP 伺服器（stdio，官方 Python SDK mcp 2.x）：把同一套查詢工具提供給 MCP 客戶端。

- 與問答機器人共用 partsqa.tools 的 Tool Registry 與權限過濾（業務邏輯只有一份）。
- 身分：啟動時以 PQA_MCP_USER / PQA_MCP_PASSWORD 登入，角色與範圍以帳號資料為準。
- 每次工具呼叫寫入稽核紀錄（event=mcp_tool）。

啟動：python mcp_server.py
"""

from __future__ import annotations

import inspect
import os
import sys

from mcp.server.mcpserver import MCPServer

from partsqa.auth import login
from partsqa.config import get_settings
from partsqa.permissions import Scope
from partsqa.service import build_agent
from partsqa.tools import TOOLS, ToolEnv, ToolSpec, run_tool


def _make_fn(spec: ToolSpec, env: ToolEnv, store):
    """依 Pydantic 參數模型產生具名參數的函式，讓 MCP SDK 自動產生 inputSchema。"""
    params = []
    for name, f in spec.args_model.model_fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.default
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=f.annotation))

    def fn(**kwargs):
        args = {k: v for k, v in kwargs.items() if v is not None}
        result, log = run_tool(env, spec.name, args)
        store.audit(username=env.ctx.username, role=env.ctx.role, event="mcp_tool", tools=[log],
                    denied_count=int(log["status"] == "denied"))
        return result

    fn.__name__ = spec.name
    fn.__signature__ = inspect.Signature(params, return_annotation=dict)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    return fn


def build_server(username: str, password: str) -> MCPServer:
    agent = build_agent(get_settings())
    ctx = login(agent.store, username, password)
    if ctx is None:
        raise PermissionError("MCP 登入失敗：請確認 PQA_MCP_USER / PQA_MCP_PASSWORD")
    env = ToolEnv(engine=agent.engine, scope=Scope(agent.engine), ctx=ctx)
    server = MCPServer("partsqa")
    for spec in TOOLS:
        server.add_tool(_make_fn(spec, env, agent.store), name=spec.name, description=spec.description)
    return server


if __name__ == "__main__":
    try:
        srv = build_server(os.getenv("PQA_MCP_USER", ""), os.getenv("PQA_MCP_PASSWORD", ""))
    except PermissionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    srv.run("stdio")
