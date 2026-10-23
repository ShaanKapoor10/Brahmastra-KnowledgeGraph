"""
Agent hooks over HTTP: POST /hooks?client=codex|cursor|gemini|claude.

The body is the client's own hook input, passed through untouched by the hook
script (plugins/*/hooks/brahmastra-hook.mjs); the response is the client's own
hook output. Everything else is in brahmastra/agent_hooks.py.

A viewer may call it (auth._VIEWER_POSTS): it reads notes and remembers the
turn's request in memory, but stores nothing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from brahmastra import agent_hooks

router = APIRouter(prefix="/hooks", tags=["hooks"])

MAX_BODY = 256_000


@router.post("")
async def agent_hook(request: Request, client: str = "claude") -> dict[str, Any]:
    if client not in agent_hooks.CLIENTS:
        raise HTTPException(400, f"unknown client {client!r}; one of {', '.join(agent_hooks.CLIENTS)}")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "hook input too large")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "hook input must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "hook input must be a JSON object")
    return await run_in_threadpool(agent_hooks.handle, client, payload)
