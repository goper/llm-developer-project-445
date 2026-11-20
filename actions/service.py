"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

prepare_* ничего не исполняют — проверяют предусловия и собирают view для
карточки («с какого на какой, что изменится»). execute_action — исполнение;
его вызывает только код оркестратора после явного «да», модели он не виден.
Субъект (sub) приходит из проверенного токена вызова — см. actions/server.py.
"""
from __future__ import annotations

import json

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from actions.contracts import (
    ActionError,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)
from core import config

MOCK = f"{config.STAND_URL}/mock"
SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]
EFFECTIVE = "с 1-го числа следующего расчётного периода"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=10)


async def _subscriber(http: httpx.AsyncClient, sub: str) -> dict:
    r = await http.get(f"{MOCK}/crm/subscriber", params={"sub": sub})
    if r.status_code == 404:
        raise ActionError("not_found", "абонент не найден")
    r.raise_for_status()
    return r.json()


async def prepare_tariff_change(sub: str, tariff_raw: str) -> dict:
    async with _client() as http:
        me = await _subscriber(http, sub)
        catalog = (await http.get(f"{MOCK}/crm/tariffs")).json()["tariffs"]

    new = normalize_tariff(tariff_raw, catalog)
    if not new["available"]:
        raise ActionError(
            "tariff_not_available",
            f"тариф «{new['name']}» закрыт для подключения",
            available=[t["name"] for t in catalog if t["available"]],
        )
    if new["name"] == me["tariff"]:
        raise ActionError("same_tariff", f"у абонента уже тариф «{me['tariff']}»")

    old = next((t for t in catalog if t["name"] == me["tariff"]), {"name": me["tariff"], "price_rub": "?"})
    return {
        "ok": True,
        "action": "tariff_change",
        "details": {"tariff": new["name"]},
        "view": {
            "что": "смена тарифа",
            "с": old["name"],
            "на": new["name"],
            "плата": f"{old['price_rub']} ₽/мес → {new['price_rub']} ₽/мес",
            "вступает в силу": EFFECTIVE,
        },
    }


async def prepare_toggle_service(sub: str, service_raw: str, enabled: bool) -> dict:
    service = normalize_service(service_raw, SERVICE_CATALOG)
    async with _client() as http:
        me = await _subscriber(http, sub)
    row = next((s for s in me.get("services", []) if s["service"] == service), None)
    is_on = bool(row and row["enabled"])
    if enabled and is_on:
        raise ActionError("service_already_on", f"услуга «{service}» уже подключена")
    if not enabled and not is_on:
        raise ActionError("service_already_off", f"услуга «{service}» уже отключена")
    price = row["price_rub"] if row else None
    return {
        "ok": True,
        "action": "service_toggle",
        "details": {"service": service, "enabled": bool(enabled)},
        "view": {
            "что": "подключение услуги" if enabled else "отключение услуги",
            "услуга": service,
            "плата": f"{price} ₽/мес" if price is not None else "по тарифу услуги",
            "вступает в силу": "сразу",
        },
    }


async def prepare_contacts_update(sub: str, phone: str | None, email: str | None) -> dict:
    if not phone and not email:
        raise ActionError("nothing_to_update", "не указан ни телефон, ни почта")
    new_phone = normalize_phone(phone) if phone else None
    new_email = normalize_email(email) if email else None
    async with _client() as http:
        me = await _subscriber(http, sub)
    if (new_phone in (None, me["phone"])) and (new_email in (None, me["email"])):
        raise ActionError("same_contacts", "эти контакты уже записаны")
    view = {"что": "смена контактов"}
    if new_phone:
        view["телефон"] = f"{me['phone']} → {new_phone}"
    if new_email:
        view["почта"] = f"{me['email']} → {new_email}"
    view["вступает в силу"] = "сразу"
    return {
        "ok": True,
        "action": "contacts_update",
        "details": {"phone": new_phone, "email": new_email},
        "view": view,
    }


async def execute_action(sub: str, action: str, details_json: str, idempotency_key: str) -> dict:
    """Исполнение после «да». Вызывает только код оркестратора."""
    try:
        details = json.loads(details_json or "{}")
    except ValueError as e:
        raise ActionError("bad_details", "details_json — не JSON") from e

    async with _client() as http:
        if action == "tariff_change":
            # идемпотентность и дневной лимит держит сам биллинг
            r = await http.post(f"{MOCK}/billing/tariff-change", json={
                "sub": sub, "tariff": details.get("tariff", ""), "idempotency_key": idempotency_key,
            })
            return r.json()
        if action == "service_toggle":
            before = await _subscriber(http, sub)
            row = next((s for s in before.get("services", []) if s["service"] == details.get("service")), None)
            r = await http.post(f"{MOCK}/crm/toggle-service", json={
                "sub": sub, "service": details.get("service", ""), "enabled": bool(details.get("enabled")),
            })
            result = r.json()
            if result.get("outcome") == "toggled":
                result.update(outcome="executed", before=bool(row and row["enabled"]), after=result.get("enabled"))
            return result
        if action == "contacts_update":
            before = await _subscriber(http, sub)
            r = await http.post(f"{MOCK}/crm/contacts", json={
                "sub": sub, "phone": details.get("phone"), "email": details.get("email"),
            })
            result = r.json()
            if result.get("outcome") == "updated":
                result.update(
                    outcome="executed",
                    before={"phone": before["phone"], "email": before["email"]},
                    after={"phone": result.get("phone"), "email": result.get("email")},
                )
            return result
    raise ActionError("unknown_action", f"неизвестное действие «{action}»",
                      available=["tariff_change", "service_toggle", "contacts_update"])


async def create_ticket(sub: str, subject: str, note: str) -> dict:
    async with _client() as http:
        r = await http.post(f"{MOCK}/tickets/create", json={
            "sub": sub, "subject": (subject or "обращение")[:200], "note": (note or "")[:1000],
        })
        return r.json()
