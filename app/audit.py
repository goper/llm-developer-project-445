"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

Журнал не редактируется: у модуля есть append и чтение, но нет update и
delete. В журнал попадают и неуспешные попытки — по нему видно «кто пытался
и что». Текст реплик целиком не пишется: в нём бывают персональные данные,
журнал — про действия, а не про переписку.

Трейс хода — три таблицы с общим trace_id: audit_log (действия, отказы,
эскалации), llm_calls (запуски агентов, app/llm.py), tool_calls (вызовы
инструментов моделью, app/wrap.py).
"""
from __future__ import annotations

import json

from core import db

OUTCOMES = frozenset({
    "executed",
    "duplicate_suppressed",
    "rejected_by_user",
    "rejected_by_contract",
    "expired",
    "escalated",
    "denied",
    "guardrail_unsafe",
    "guardrail_degraded",
    "token_refreshed",
    "server_limit",
    "budget_exhausted",
    "error",
})

ARGS_LIMIT = 500


def _j(value) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text


def append(
    who: str,
    what: str,
    outcome: str,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    args: dict | None = None,
    before=None,
    after=None,
    idempotency_key: str | None = None,
    detail: str | None = None,
) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"неизвестный исход аудита: {outcome}")
    db.execute(
        "insert into audit_log(trace_id, session_id, who, what, outcome, args, before, after,"
        " idempotency_key, detail) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            trace_id, session_id, who, what, outcome,
            _j(args), _j(before), _j(after), idempotency_key,
            (detail or "")[:300] or None,
        ),
    )


def tail(n: int = 20, who: str | None = None) -> list[dict]:
    if who:
        return db.fetchall("select * from audit_log where who = %s order by id desc limit %s", (who, n))
    return db.fetchall("select * from audit_log order by id desc limit %s", (n,))


def trace(trace_id: str) -> list[dict]:
    """Хронология хода по трём журналам."""
    return db.fetchall(
        "select ts, 'audit' as src, who as name, outcome, detail from audit_log where trace_id = %s"
        " union all select ts, 'llm', agent, model, latency_ms::text from llm_calls where trace_id = %s"
        " union all select ts, 'tool', tool, outcome, latency_ms::text from tool_calls where trace_id = %s"
        " order by ts",
        (trace_id, trace_id, trace_id),
    )


def tool_call(trace_id: str, tool: str, args: dict | None, outcome: str, latency_ms: int) -> None:
    """Вызов инструмента моделью — в tool_calls (аргументы обрезаны)."""
    text = _j(args or {}) or "{}"
    if len(text) > ARGS_LIMIT:
        text = json.dumps({"truncated": text[:ARGS_LIMIT]}, ensure_ascii=False)
    try:
        db.execute(
            "insert into tool_calls(trace_id, tool, args, outcome, latency_ms) values (%s, %s, %s, %s, %s)",
            (trace_id, tool, text, outcome, latency_ms),
        )
    except Exception:  # noqa: BLE001 — журнал не должен ронять ход
        pass
