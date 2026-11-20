"""Права сессии: allowlist из claims, два барьера оркестратора.

Роль задаёт базовый набор операций, scope его сужает:
  - client с tariffs:write — чтение + карточные действия (prepare_*);
  - client без него — только чтение;
  - operator — чтение + обзор абонентов, ни одной операции записи.
create_ticket доступен всем: позвать человека можно всегда.

Барьер 1 (visible_tools) — при сборке агента: модель не видит запрещённого.
Барьер 2 (admit) — в момент вызова: операция вне allowlist или
идентификатор объекта в аргументах — отказ. execute_action не виден
никому и не допускается ни для кого: его вызывает код после «да».
"""
from __future__ import annotations

from dataclasses import dataclass, field

READ_OPS = ("get_my_profile", "get_my_tariff", "list_tariffs", "list_my_charges", "search_kb")
WRITE_OPS = ("prepare_tariff_change", "prepare_toggle_service", "prepare_contacts_update")
OPERATOR_OPS = ("get_subscriber_overview",)  # чужой идентификатор легитимен: роль проверит сервер
ESCALATION_OPS = ("create_ticket",)
CODE_ONLY_OPS = ("execute_action",)  # не видит ни одна модель
# phone не ключ идентичности: в prepare_contacts_update это новое значение контакта
IDENTITY_ARGS = ("user_id", "account", "subscriber", "msisdn", "sub")

WRITE_SCOPE = "tariffs:write"


@dataclass
class Allowlist:
    sub: str
    role: str
    scopes: list[str] = field(default_factory=list)
    read_ops: list[str] = field(default_factory=list)
    write_ops: list[str] = field(default_factory=list)

    @property
    def ops(self) -> set[str]:
        return set(self.read_ops) | set(self.write_ops)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def allowlist_from_claims(claims: dict) -> Allowlist:
    role = claims.get("role", "")
    scope = claims.get("scope", "")
    scopes = scope.split() if isinstance(scope, str) else list(scope or [])
    read_ops: list[str] = list(ESCALATION_OPS)
    write_ops: list[str] = []
    if role == "client":
        read_ops += READ_OPS
        if WRITE_SCOPE in scopes:
            write_ops += WRITE_OPS
    elif role == "operator":
        read_ops += READ_OPS + OPERATOR_OPS
    return Allowlist(sub=claims.get("sub", ""), role=role, scopes=scopes, read_ops=read_ops, write_ops=write_ops)


def visible_tools(allowlist: Allowlist, names) -> list[str]:
    """Барьер 1: из перечня имён — только разрешённые этой сессии."""
    allowed = allowlist.ops
    return [n for n in names if n in allowed and n not in CODE_ONLY_OPS]


def admit(allowlist: Allowlist, operation: str, args: dict | None) -> Decision:
    """Барьер 2: допуск тройки «субъект, операция, объект» в момент вызова."""
    if operation in CODE_ONLY_OPS or operation not in allowlist.ops:
        return Decision(False, f"operation_not_allowed:{operation}")
    if operation not in OPERATOR_OPS:
        for key in IDENTITY_ARGS:
            if key in (args or {}):
                return Decision(False, f"object_from_arguments:{key}")
    return Decision(True)
