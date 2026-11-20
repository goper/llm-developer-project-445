"""Идемпотентность исполнения на стороне оркестратора.

Ключ в оркестраторе — f"{action}:{pending_id}": одно «да» — одна операция.
  - ключ в таблице idempotency с результатом → StoredResult(duplicate=True,
    сохранённый результат): повтор не исполняет операцию второй раз;
  - иначе исполняется fn(); неудачный исход (error / не executed) ключ не
    занимает — сбой не должен запирать повторную попытку;
  - успех → INSERT ... ON CONFLICT (key) DO NOTHING.
Для смены тарифа дедупликация есть и на стороне биллинга — два барьера;
для услуг и контактов этот слой единственный.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core import db


@dataclass
class StoredResult:
    duplicate: bool
    result: dict


def lookup(key: str) -> dict | None:
    row = db.fetchone("select result from idempotency where key = %s", (key,))
    if row is None or row["result"] is None:
        return None
    return row["result"]


def _succeeded(result: dict) -> bool:
    return "error" not in result and result.get("outcome", "executed") in ("executed", "duplicate")


async def execute(key: str, sub: str, action: str, fn: Callable[[], Awaitable[dict]]) -> StoredResult:
    stored = lookup(key)
    if stored is not None:
        return StoredResult(duplicate=True, result=stored)

    result = await fn()
    if not isinstance(result, dict):
        result = {"raw": result}
    if not _succeeded(result):
        return StoredResult(duplicate=False, result=result)  # ключ не занимаем

    db.execute(
        "insert into idempotency(key, sub, action, result) values (%s, %s, %s, %s)"
        " on conflict (key) do nothing",
        (key, sub, action, json.dumps(result, ensure_ascii=False, default=str)),
    )
    return StoredResult(duplicate=result.get("outcome") == "duplicate", result=result)
