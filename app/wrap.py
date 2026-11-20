"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

MCP-инструмент превращается в FunctionTool агента; субъект вызова — в
Bearer-токене соединения; кривой JSON аргументов — машинно-читаемый ответ
модели, сессия не роняется; ошибка MCP-сервера (MCPError, код + data с
подсказками) — JSON tool-результат для модели.

Два барьера прав:
  wrap_tools — барьер 1: модель не видит запрещённого (permissions.visible_tools);
  invoke     — барьер 2: permissions.admit перед каждым вызовом; отказ →
               {"error": "denied", "reason"} + запись в аудит.
Крючок prepare_*: успешная подготовка → карточка (cards.create) → решение
политики Prepare (pep.prepare). Deny — карточки нет, модель получает отказ.
Каждый вызов модели → tool_calls (trace_id, tool, args, outcome, latency_ms).
"""
from __future__ import annotations

import json
import time

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import audit, cards, permissions
from app.sessions import Session, pep_of
from core import config


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _collect_sources(session: Session, name: str, result: dict) -> None:
    if name == "search_kb":
        for hit in result.get("results", []) or []:
            src = hit.get("source")
            if src and src not in session.turn_sources:
                session.turn_sources.append(src)


async def call_tool(session: Session, name: str, args: dict) -> dict:
    """Вызов MCP-инструмента; ошибка сервера превращается в данные для модели."""
    try:
        result = await session.pool.call(session.pool.resource_of(name), name, args)
    except MCPError as e:
        data = e.data if isinstance(e.data, dict) else {}
        message = getattr(getattr(e, "error", None), "message", None) or str(e) or "tool_failed"
        return {"error": data.get("error") or message[:60], **data}
    except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
        return {"error": "tool_failed", "detail": str(e)[:160]}
    return result if isinstance(result, dict) else {"result": result}


async def _on_prepare(session: Session, name: str, result: dict) -> dict:
    """prepare_* ok → карточка → политика Prepare → ответ модели."""
    card = cards.create(session, result["action"], result.get("details", {}), result.get("view", {}))
    decision = await pep_of(session).prepare(card)
    if not decision.allowed:
        cards.settle(session, card, "denied")
        audit.append(session.user.sub, result["action"], "denied", trace_id=session.trace_id,
                     session_id=session.session_id, detail=decision.reason)
        return {"error": "denied", "reason": decision.reason,
                "detail": "изменение недоступно: скажи абоненту, что сейчас его выполнить нельзя, и предложи позвать оператора"}
    session.new_card = card
    return {
        "card_created": True,
        "card": card.public_view(config.CARD_TTL_SEC),
        "next": "перескажи карточку и спроси подтверждение «да/нет»; сам ничего не исполняй",
    }


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""

    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        started = time.monotonic()

        def log(outcome: str, args=None) -> None:
            audit.tool_call(session.trace_id, name, args, outcome, int((time.monotonic() - started) * 1000))

        try:
            args = json.loads(input_json) if input_json and input_json.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except (TypeError, ValueError):
            log("bad_arguments", {"raw": (input_json or "")[:200]})
            return _dump({"error": "bad_arguments", "detail": "аргументы — не JSON-объект; повтори вызов по схеме"})

        decision = permissions.admit(session.allowlist, name, args)
        if not decision.allowed:
            log("denied", args)
            audit.append(session.user.sub, name, "denied", trace_id=session.trace_id,
                         session_id=session.session_id, args=args, detail=decision.reason)
            return _dump({"error": "denied", "reason": decision.reason})

        session.note(name)
        result = await call_tool(session, name, args)
        if "error" in result:
            log(f"error:{result['error']}"[:40], args)
            return _dump(result)

        _collect_sources(session, name, result)
        if name.startswith("prepare_") and result.get("ok"):
            reply = await _on_prepare(session, name, result)
            log("card_created" if reply.get("card_created") else f"denied:{reply.get('reason')}"[:40], args)
            return _dump(reply)

        log("ok", args)
        return _dump(result)

    return FunctionTool(
        name=name,
        description=session.pool.tool_description(name),
        params_json_schema=session.pool.tool_schema(name),
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Барьер 1: только известные пулу и разрешённые пользователю инструменты."""
    known = set(session.pool.tool_names())
    allowed = permissions.visible_tools(session.allowlist, [n for n in names if n in known])
    return [wrap_mcp_tool(session, n) for n in allowed]
