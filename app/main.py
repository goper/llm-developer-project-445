"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  GET  /api/health
  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  {"session_id", "answer": "yes"|"no"} — кнопка карточки
  GET  /api/trace/{trace_id}             хронология хода по журналам (разбор инцидента)

События SSE хода: plan / card / reply / error / done.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import audit, orchestrator, sessions
from core import config

log = logging.getLogger("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # LLM нужен только живым ходам; smoke и исполнение карточек работают без модели
    try:
        config.setup_model()
        if os.environ.get("LLM_SECONDARY_BASE_URL"):
            try:
                from app import llm_pool

                llm_pool.setup_pool()
            except ImportError:
                log.warning("LLM_SECONDARY_BASE_URL задан, но app/llm_pool.py нет")
    except SystemExit as e:
        log.warning("LLM не настроена: %s", e)
    yield
    for s in list(sessions.SESSIONS.values()):
        await s.pool.close()


app = FastAPI(title="operator-chat backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SessionIn(BaseModel):
    user: str


class ChatIn(BaseModel):
    session_id: str
    message: str


class DecisionIn(BaseModel):
    session_id: str
    answer: str


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "backend", "model": config.MODEL_NAME or None}


@app.post("/api/session")
async def open_session(body: SessionIn):
    if body.user not in config.USERS:
        return JSONResponse({"error": "unknown_user", "available": sorted(config.USERS)}, status_code=400)
    try:
        session = await sessions.open_session(body.user)
    except KeyError:
        return JSONResponse({"error": "unknown_user"}, status_code=400)
    return {
        "session_id": session.session_id,
        "name": session.user.name,
        "role": session.user.role,
        "visible_tools": sessions.visible_tools(session),
    }


def _session(session_id: str):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "нет такой сессии: откройте новую")
    return session


@app.post("/api/chat")
async def chat(body: ChatIn):
    session = _session(body.session_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def work() -> None:
        try:
            await orchestrator.run_turn(session, body.message, emit)
        except Exception as e:  # noqa: BLE001 — сбой хода — событие, а не обрыв потока
            log.exception("ход упал")
            try:
                audit.append(session.user.sub, "turn", "error", trace_id=session.trace_id,
                             session_id=session.session_id, detail=repr(e)[:200])
            except Exception:  # noqa: BLE001
                pass
            await queue.put({"type": "error", "detail": str(e)[:200] or e.__class__.__name__})
        finally:
            await queue.put({"type": "done", "trace_id": session.trace_id})

    async def stream():
        task = asyncio.create_task(work())
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/cards/{pending_id}/decision")
async def decision(pending_id: str, body: DecisionIn):
    session = _session(body.session_id)
    answer = "yes" if body.answer.strip().lower() in ("yes", "да", "true", "1") else "no"
    result = await orchestrator.decide_card(session, pending_id, answer)
    return {**result, "trace_id": session.trace_id}


@app.get("/api/trace/{trace_id}")
async def trace(trace_id: str):
    return {"trace_id": trace_id, "events": audit.trace(trace_id)}
