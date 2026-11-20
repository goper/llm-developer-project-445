"""Контракты действий: схемы, нормализация, таксономия ошибок.

Три класса исходов действия:
  - ошибка валидации (unknown_tariff, unknown_service, bad_phone, ...) —
    машинно-читаемый ответ с подсказкой, модель исправляет вызов;
  - отказ по бизнес-правилу (BUSINESS_RULE_CODES) — повторять бессмысленно,
    модель пересказывает отказ абоненту;
  - сбой инфраструктуры — до модели не доходит: core/mcpclient.py повторяет вызов.
"""
from __future__ import annotations

import re

from pydantic import BaseModel


class ActionError(Exception):
    """Отказ действия: код + человекочитаемое сообщение + подсказки для модели."""

    def __init__(self, code: str, message: str, **hints) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints

    def to_tool_result(self) -> dict:
        return {"error": self.code, "detail": self.message, **self.hints}

    @property
    def is_business_rule(self) -> bool:
        return self.code in BUSINESS_RULE_CODES


# Отказы по правилам бизнеса: запрос понятен, но выполнить его нельзя.
BUSINESS_RULE_CODES = frozenset({
    "same_tariff",
    "tariff_not_available",
    "service_already_on",
    "service_already_off",
    "same_contacts",
    "nothing_to_update",
})


class PreparedAction(BaseModel):
    """Результат prepare_*: что исполнить потом и что показать в карточке."""

    ok: bool = True
    action: str
    details: dict
    view: dict


_QUOTES = "«»\"'„“” "


def _clean(raw: str) -> str:
    text = (raw or "").strip().strip(_QUOTES).lower().replace("ё", "е")
    for prefix in ("тариф ", "тарифа ", "тарифу ", "тарифом ", "услуга ", "услугу ", "услуги "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().strip(_QUOTES)


def _match(wanted: str, name: str) -> bool:
    """«семейный», «семейного», «Семейный тариф» → Семейный; «про» — только целиком."""
    target = name.lower().replace("ё", "е")
    if wanted == target:
        return True
    words = wanted.split()
    if len(target) <= 4:  # короткие названия (Про) — только точное слово
        return target in words
    stem = target[: max(4, len(target) - 2)]
    return any(w.startswith(stem) for w in words)


def normalize_tariff(raw: str, catalog: list[dict]) -> dict:
    """Название тарифа «как услышала модель» → запись каталога."""
    wanted = _clean(raw)
    for item in catalog:
        if _match(wanted, item["name"]):
            return item
    raise ActionError(
        "unknown_tariff",
        f"тарифа «{raw}» нет в каталоге",
        available=[t["name"] for t in catalog if t.get("available", True)],
    )


def normalize_service(raw: str, catalog: list[str]) -> str:
    wanted = _clean(raw)
    for name in catalog:
        if _match(wanted, name):
            return name
    raise ActionError("unknown_service", f"услуги «{raw}» нет", available=list(catalog))


def normalize_phone(raw: str) -> str:
    """Любая запись номера → +7 XXX XXX-XX-XX (формат CRM)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] in "78":
        d = "7" + digits[1:]
        return f"+{d[0]} {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}"
    raise ActionError("bad_phone", f"номер «{raw}» не похож на российский", example="+7 900 111-22-33")


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zа-я]{2,}", email):
        raise ActionError("bad_email", f"адрес «{raw}» не похож на почту", example="anna@example.ru")
    return email
