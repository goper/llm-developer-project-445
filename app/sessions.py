"""Сессии чата: identity, соединения с MCP, история планировщика.

Одна сессия = один пользователь чата. При открытии сессии backend —
доверенный клиент — выпускает у издателя токены для всех MCP-серверов
(по одному на resource, RFC 8707) и держит открытые соединения.

claims токена определяют allowlist (app.permissions); pep — клиент PDP
(app.pep, создаётся лениво через pep_of); guard_state — вердикт guardrail
текущего хода: ok=False закрывает действия в политике (guard_ok).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app import permissions
from core import config, db
from core.mcpclient import McpPool
from core.tokens import Tokens


@dataclass
class Session:
    login: str
    user: config.User
    tokens: Tokens
    pool: McpPool
    thread: list = field(default_factory=list)   # история планировщика
    trace: list[str] = field(default_factory=list)
    trace_id: str = ""                           # сквозной id текущего хода
    planner: object | None = None                # собирается лениво на первом ходе
    pending_card: object | None = None           # живая карточка подтверждения
    last_card: object | None = None              # последняя исполненная карточка (повторное «да»)
    new_card: object | None = None               # карточка, созданная на этом ходу
    turn_sources: list[str] = field(default_factory=list)
    claims: dict = field(default_factory=dict)
    allowlist: permissions.Allowlist | None = None
    pep: object | None = None
    guard_state: dict = field(default_factory=lambda: {"ok": True})
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:8]}")

    def note(self, tool: str) -> None:
        self.trace.append(tool)


SESSIONS: dict[str, Session] = {}


async def open_session(login: str) -> Session:
    user = config.USERS.get(login)
    if user is None:
        raise KeyError(f"неизвестный пользователь: {login}")

    tokens = Tokens(login)
    pool = McpPool(tokens, resource_keys=("crm", "billing", "kb", "actions"))
    await pool.connect()

    claims = await tokens.claims("crm")
    session = Session(login=login, user=user, tokens=tokens, pool=pool, claims=claims,
                      allowlist=permissions.allowlist_from_claims(claims))
    db.execute(
        "insert into sessions(session_id, login, sub) values (%s, %s, %s)",
        (session.session_id, login, user.sub),
    )
    SESSIONS[session.session_id] = session
    return session


def get(session_id: str) -> Session | None:
    return SESSIONS.get(session_id)


def visible_tools(session: Session) -> list[str]:
    return permissions.visible_tools(session.allowlist, session.pool.tool_names())


def pep_of(session: Session):
    """PEP сессии: создаётся при первом обращении, дальше — тот же."""
    if session.pep is None:
        from app.pep import Pep

        session.pep = Pep(session.session_id, session.claims, session.guard_state)
    return session.pep
