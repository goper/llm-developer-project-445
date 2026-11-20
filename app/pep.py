"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

Цикл карточки в политике: Prepare → ConfirmCard → Execute.
  prepare(card) — Allow фиксирует карточку в журнале PDP; Deny — карточки нет;
  confirm(card) — «да» по этой карточке; TTL карточки PDP проверяет по часам;
  execute(card) — окно formerly ConfirmCard + forbid-лимит.
Субъектные факты — из проверенного токена: sub, role, scopes.
Недоступный PDP — fail-closed: Decision(False, "pdp_unavailable").
"""
from __future__ import annotations

import json
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


class Pep:
    def __init__(self, session_id: str, claims: dict, guard_state: dict) -> None:
        self.session_id = session_id
        self.guard_state = guard_state
        scope = claims.get("scope", "")
        self.subject = {
            "sub": claims.get("sub", ""),
            "role": claims.get("role", ""),
            "scopes": scope.split() if isinstance(scope, str) else list(scope or []),
        }
        self._opened = False

    async def _open(self, client) -> None:
        r = await client.post(f"{config.PDP_URL}/session",
                              json={"session_id": self.session_id, "subject": self.subject})
        r.raise_for_status()
        self._opened = True

    def _input(self, card) -> dict:
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            "details": json.dumps(card.details, ensure_ascii=False, sort_keys=True),
            "guard_ok": bool(self.guard_state.get("ok", True)),
            **self.subject,
        }

    async def _ask(self, path: str, body: dict) -> Decision:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                for _ in range(2):
                    if not self._opened:
                        await self._open(client)
                    r = await client.post(f"{config.PDP_URL}{path}", json={"session_id": self.session_id, **body})
                    if r.status_code != 404:
                        break
                    self._opened = False  # PDP перезапускали — сессии нет, открываем заново
                data = r.json()
        except (httpx.HTTPError, ValueError):
            return Decision(False, "pdp_unavailable")
        if "decision" not in data:
            return Decision(False, data.get("error") or "pdp_error")
        return Decision(data["decision"] == "Allow", data.get("reason", ""))

    async def prepare(self, card) -> Decision:
        return await self._ask("/authorize", {"action": "Prepare", "input": self._input(card)})

    async def confirm(self, card) -> Decision:
        return await self._ask("/confirm", {"action": "ConfirmCard", "input": self._input(card)})

    async def execute(self, card) -> Decision:
        return await self._ask("/authorize", {"action": "Execute", "input": self._input(card)})
