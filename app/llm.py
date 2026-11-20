"""Сборка агентов на модели из .env + учёт LLM-вызовов.

build_agent — Agent из agents SDK; output_type подключается нестрогой
схемой: строгий JSON-режим слишком требователен к малым моделям.
run_agent — отдельный Runner.run на задачу; каждый запуск ложится в
llm_calls (trace_id, агент, модель, токены, латентность).
"""
from __future__ import annotations

import time

from agents import Agent, AgentOutputSchema, Runner

from core import config, db

MAX_TURNS = 8


def model_name() -> str:
    if not config.MODEL_NAME:
        config.setup_model()
    return config.MODEL_NAME


def build_agent(name: str, instructions: str, tools=None, output_type=None) -> Agent:
    kwargs = {}
    if output_type is not None:
        kwargs["output_type"] = AgentOutputSchema(output_type, strict_json_schema=False)
    return Agent(name=name, instructions=instructions, tools=list(tools or []), model=model_name(), **kwargs)


def _record(trace_id: str, agent: str, usage, latency_ms: int, model: str | None = None) -> None:
    try:
        db.execute(
            "insert into llm_calls(trace_id, agent, model, input_tokens, output_tokens, latency_ms)"
            " values (%s, %s, %s, %s, %s, %s)",
            (trace_id, agent, model or config.MODEL_NAME,
             getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None), latency_ms),
        )
    except Exception:  # noqa: BLE001 — журнал не должен ронять ход
        pass


def tool_calls_of(items) -> list[dict]:
    """call_id → {name, arguments, output} из result.new_items."""
    calls: dict[str, dict] = {}
    for item in items or []:
        raw = getattr(item, "raw_item", None)
        kind = getattr(item, "type", "")
        if kind == "tool_call_item":
            call_id = getattr(raw, "call_id", None) or getattr(raw, "id", "")
            calls.setdefault(call_id, {})
            calls[call_id].update(name=getattr(raw, "name", ""), arguments=getattr(raw, "arguments", ""))
        elif kind == "tool_call_output_item":
            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", "")
            calls.setdefault(call_id, {})["output"] = getattr(item, "output", None)
    return list(calls.values())


async def run(agent: Agent, task, *, trace_id: str = ""):
    """Runner.run с записью в llm_calls; возвращает RunResult."""
    from app import llm_pool

    started = time.monotonic()
    model = None
    if llm_pool.enabled():  # бонус: failover между провайдерами, в журнал — фактический
        provider, result = await llm_pool.run_with_failover(agent, task, max_turns=MAX_TURNS)
        model = f"{provider.name}:{provider.model}"
    else:
        result = await Runner.run(agent, task, max_turns=MAX_TURNS)
    latency_ms = int((time.monotonic() - started) * 1000)
    _record(trace_id, agent.name, result.context_wrapper.usage, latency_ms, model)
    return result


async def run_agent(agent: Agent, task: str, *, trace_id: str = "") -> dict:
    started = time.monotonic()
    result = await run(agent, task, trace_id=trace_id)
    return {
        "report": str(result.final_output or "").strip(),
        "tools": tool_calls_of(result.new_items),
        "seconds": round(time.monotonic() - started, 2),
    }
