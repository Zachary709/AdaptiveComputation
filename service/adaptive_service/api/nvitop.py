from __future__ import annotations

import html
import json
import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .common import find_node, latest_snapshot

router = APIRouter(tags=["nvitop"])

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _node(request: Request, alias: str) -> dict:
    node = find_node(latest_snapshot(request), alias)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {alias}")
    node_alias = str(node.get("alias") or "")
    if not _ALIAS_PATTERN.fullmatch(node_alias):
        raise HTTPException(status_code=400, detail=f"invalid node alias: {node_alias}")
    if node.get("reachable") is False or node.get("stale"):
        raise HTTPException(status_code=502, detail=f"node is unreachable: {node_alias}")
    return node


@router.get("/nodes/{alias}/nvitop")
async def node_nvitop(
    alias: str,
    request: Request,
    retry: bool = Query(default=False),
) -> JSONResponse:
    node = _node(request, alias)
    node_alias = str(node["alias"])
    manager = request.app.state.nvitop
    cached = manager.cached(node_alias)
    error = manager.error(node_alias)
    if cached is not None and (not retry or error is None):
        return JSONResponse(cached)
    if not retry and error is not None:
        return JSONResponse({"error": error}, status_code=502)
    try:
        payload = await manager.retry(node)
    except (OSError, RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(payload)


@router.get("/nodes/{alias}/gpu-state")
def node_gpu_state(alias: str, request: Request) -> JSONResponse:
    node = _node(request, alias)
    settings = request.app.state.settings
    try:
        payload = request.app.state.nvitop.states(
            str(node["alias"]),
            window_s=settings.gpu_idle_window_s,
            max_memory_mib=settings.gpu_idle_max_memory_mib,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(payload)


@router.get("/nodes/{alias}/nvitop/text", response_class=PlainTextResponse)
async def node_nvitop_text(
    alias: str,
    request: Request,
) -> PlainTextResponse:
    node = _node(request, alias)
    try:
        output = await request.app.state.nvitop.run_text(node)
    except (OSError, RuntimeError) as exc:
        return PlainTextResponse(f"读取 nvitop 失败：{exc}", status_code=502)
    return PlainTextResponse(output)


def nvitop_page(alias: str) -> str:
    escaped = html.escape(alias)
    alias_json = json.dumps(alias)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nvitop · {escaped}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    margin: 0;
    padding: 16px;
    background: #f6f8fa;
    color: #1f2328;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}
  header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 12px;
  }}
  h1 {{ margin: 0; font-size: 16px; }}
  .meta {{ color: #59636e; font-size: 12px; }}
  pre {{
    margin: 0;
    padding: 12px;
    min-height: 240px;
    overflow: auto;
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.3;
  }}
  .error {{ color: #b42318; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>nvitop · {escaped}</h1>
    <div class="meta" id="meta">每 5 秒刷新</div>
  </div>
</header>
<pre id="output">读取中...</pre>
<script>
const alias = {alias_json};
const output = document.getElementById("output");
const meta = document.getElementById("meta");

async function load() {{
  try {{
    const response = await fetch(`/api/v1/nodes/${{encodeURIComponent(alias)}}/nvitop/text`);
    const text = await response.text();
    output.textContent = text;
    output.classList.toggle("error", !response.ok);
    meta.textContent = response.ok
      ? `更新于 ${{new Date().toLocaleTimeString()}} · 每 5 秒刷新`
      : "读取失败";
  }} catch (error) {{
    output.textContent = `读取失败：${{error}}`;
    output.classList.add("error");
    meta.textContent = "读取失败";
  }}
}}

load();
window.setInterval(load, 5000);
</script>
</body>
</html>
"""
