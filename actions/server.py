"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

OAuth 2.1 resource server: каждый вызов несёт Bearer-токен, сервер сам
проверяет подпись по JWKS, издателя, адресата (RFC 8707) и срок. Субъект
действия берётся из проверенного токена (get_access_token().subject) —
аргумента sub в контрактах нет, подменять нечего (структурная защита от IDOR).

Для модели: prepare_tariff_change / prepare_toggle_service /
prepare_contacts_update — только подготовка карточки, и create_ticket.
execute_action — НЕ для модели: его вызывает код оркестратора после «да»
(в allowlist ни одного агента он не попадает).
ActionError → MCPError(code=-32002, message=code, data=hints): подсказки
доезжают до модели.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from actions import service as svc
from actions.contracts import ActionError
from core.config import RESOURCES
from core.server_auth import make_mcp_server

ACTION_ERROR_CODE = -32002

actions = make_mcp_server(
    "actions",
    resource=RESOURCES["actions"],
    instructions=(
        "Действия абонента мобильного оператора. prepare_* только готовят карточку "
        "подтверждения и ничего не меняют. Субъект определяется токеном вызова."
    ),
)


def _subject() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise MCPError(code=-32001, message="unauthorized", data={"error": "unauthorized"})
    return token.subject


async def _run(coro):
    try:
        return await coro
    except ActionError as e:
        raise MCPError(code=ACTION_ERROR_CODE, message=e.code, data=e.to_tool_result()) from e


@actions.tool()
async def prepare_tariff_change(tariff: str) -> dict:
    """Подготовить смену тарифа абонента: проверка и карточка подтверждения.

    Ничего не меняет. tariff — название тарифа, как его назвал абонент
    (например «семейный»). Ошибка unknown_tariff содержит список available.
    """
    return await _run(svc.prepare_tariff_change(_subject(), tariff))


@actions.tool()
async def prepare_toggle_service(service: str, enabled: bool) -> dict:
    """Подготовить подключение (enabled=true) или отключение (enabled=false) услуги.

    Услуги: Музыка, Роуминг, Антивирус. Ничего не меняет — только карточка.
    """
    return await _run(svc.prepare_toggle_service(_subject(), service, enabled))


@actions.tool()
async def prepare_contacts_update(phone: str | None = None, email: str | None = None) -> dict:
    """Подготовить смену контактов абонента: новый телефон и/или почта.

    Ничего не меняет — только карточка подтверждения.
    """
    return await _run(svc.prepare_contacts_update(_subject(), phone, email))


@actions.tool()
async def execute_action(action: str, details_json: str, pending_id: str, idempotency_key: str) -> dict:
    """Исполнение подтверждённой карточки. НЕ для модели: вызывает код оркестратора."""
    return await _run(svc.execute_action(_subject(), action, details_json, idempotency_key))


@actions.tool()
async def create_ticket(subject: str, note: str = "") -> dict:
    """Эскалация: создать обращение к живому оператору поддержки."""
    return await _run(svc.create_ticket(_subject(), subject, note))


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "actions"})


@asynccontextmanager
async def lifespan(app):
    """Session manager MCP обязан жить в event loop uvicorn."""
    async with actions.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/actions", app=actions.streamable_http_app()),
    ],
    lifespan=lifespan,
)
