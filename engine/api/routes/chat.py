"""Chat route — SSE streaming over ontorag AgentLoop.

AgentLoop API:
  AgentLoop(store, llm, has_ontology_data=False)
  await loop.run(user_message) → AsyncGenerator[dict, None]

세션 관리: OrderedDict + 최대 100개 cap (메모리 누수 방지).
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from ontorag.chat.agent import AgentLoop
from ontorag.llm.factory import get_llm_provider
from ontorag.stores.factory import create_store

from engine.query.router import QueryType, route_question

router = APIRouter(prefix="/ui", tags=["ui"])
logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent.parent.parent.parent / "web" / "templates")
)

_MAX_SESSIONS = 100
_sessions: OrderedDict[str, AgentLoop] = OrderedDict()


async def _get_or_create_loop(session_id: str | None) -> tuple[str, AgentLoop]:
    import uuid
    sid = session_id or str(uuid.uuid4())

    if sid in _sessions:
        _sessions.move_to_end(sid)
        return sid, _sessions[sid]

    store = create_store()
    llm = get_llm_provider()

    # 그래프에 데이터가 있으면 AgentLoop이 tool 사용을 강제한다.
    has_data = False
    try:
        schema = await store.get_schema()
        has_data = any(cls.instance_count > 0 for cls in schema.classes)
    except Exception:
        pass

    loop = AgentLoop(store=store, llm=llm, has_ontology_data=has_data)

    if len(_sessions) >= _MAX_SESSIONS:
        _sessions.popitem(last=False)  # 가장 오래된 세션 제거
    _sessions[sid] = loop
    return sid, loop


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "chat.html")


@router.post("/chat/stream")
async def chat_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="요청 본문이 유효한 JSON이 아닙니다")
    question: str = body.get("question", "").strip()
    session_id: str | None = body.get("session_id")

    if not question:
        async def empty():
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    query_type = route_question(question)
    sid, loop = await _get_or_create_loop(session_id)

    effective_q = question
    if query_type == QueryType.INCREMENTAL:
        effective_q = f"[HINT: filter by urn:pg:ingestedAt recent values] {question}"

    async def generate():
        yield f"data: {json.dumps({'type': 'session', 'session_id': sid})}\n\n"
        try:
            async for event in loop.run(effective_q):
                if await request.is_disconnected():
                    logger.info("Client disconnected, stopping stream sid=%s", sid)
                    break
                yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception:
            logger.exception("Chat stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': '처리 중 오류가 발생했습니다'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
