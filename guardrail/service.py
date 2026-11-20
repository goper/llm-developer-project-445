"""Guardrail-сервис: отдельный stateless-контур проверки текста.

    uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300

POST /check {"text", "mode": "input"|"output"} →
    {"verdict": "safe"|"unsafe", "category", "engine", "mode"}
POST /admin/down, /admin/up — имитация отказа сервиса; GET /health — 503 при down.

Движок по умолчанию — RULE: детерминированные маркеры атак стенда. Это
лабораторная эвристика, для прода её мало. GUARDRAIL_MODEL=1 включает
классификатор HiveTraceGuard-Pro (transformers+torch, в requirements нет).

Guardrail только ставит вердикт по тексту. Права решают барьеры и политика:
недоступность сервиса оркестратор превращает во флаг guard_ok=false, и
политика закрывает действия (fail-closed), а чтение продолжается.
"""
from __future__ import annotations

import os
import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Категория → маркеры (регулярные выражения по тексту в нижнем регистре, ё → е).
RULE_PATTERNS: dict[str, list[str]] = {
    "jailbreak": [
        r"суперпользовател",
        r"ты\s+теперь\s+(админ|администратор|root|разработчик)",
        r"режим\s+(разработчика|бога|без\s+ограничений)",
        r"отключи\s+(все\s+)?(проверки|фильтры|ограничения)",
        r"\bdan\b|do anything now",
    ],
    "prompt_injection": [
        r"игнорируй\s+(все\s+|свои\s+|предыдущие\s+|прошлые\s+)*(инструкц|правил|указан)",
        r"забудь\s+(все\s+|свои\s+|предыдущие\s+)*(инструкц|правил|указан)",
        r"ignore\s+(all\s+|previous\s+|prior\s+)*instructions",
        r"системн\w*\s+(промпт|инструкц)",
        r"покажи\s+(свой\s+)?(промпт|системн)",
    ],
    "tool_hijack": [
        r"смени\s+тариф\s+в\s+аккаунте",
        r"execute_action",
        r"вызови\s+(инструмент|функцию)",
        r"(для|у)\s+абонента\s+u_\w+",
        r"\bu_[0-9a-z]{3,}\b.*(тариф|услуг|контакт)",
    ],
    "injected_instruction": [
        r"немедленно\s+переведи",
        r"без\s+(подтверждения|согласия|карточки)",
        r"срочное\s+указание",
        r"===\s*системная",
    ],
}
_COMPILED = {cat: [re.compile(p, re.IGNORECASE) for p in ps] for cat, ps in RULE_PATTERNS.items()}

STATE = {"down": False}


def _normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def classify_rule(text: str) -> str | None:
    """Первая сработавшая категория или None (текст безопасен)."""
    norm = _normalize(text)
    for category, patterns in _COMPILED.items():
        if any(p.search(norm) for p in patterns):
            return category
    return None


_MODEL = None


def classify_model(text: str) -> str | None:  # pragma: no cover — опциональный движок
    global _MODEL
    if _MODEL is None:
        from transformers import pipeline

        _MODEL = pipeline("text-classification", model="hivetrace/HiveTraceGuard-Pro")
    label = _MODEL(text[:2000])[0]["label"].lower()
    return None if label in ("safe", "benign", "label_0") else "model_unsafe"


ENGINE = "model" if os.environ.get("GUARDRAIL_MODEL") == "1" else "rule"


async def check(request: Request) -> JSONResponse:
    if STATE["down"]:
        return JSONResponse({"error": "unavailable"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    text = str(body.get("text", ""))
    mode = body.get("mode", "input")
    if mode not in ("input", "output"):
        return JSONResponse({"error": "bad_mode", "available": ["input", "output"]}, status_code=400)

    category = classify_rule(text)
    engine = "rule"
    if category is None and ENGINE == "model":
        category, engine = classify_model(text), "model"
    return JSONResponse({
        "verdict": "unsafe" if category else "safe",
        "category": category,
        "engine": engine,
        "mode": mode,
    })


async def admin_down(request: Request) -> JSONResponse:
    STATE["down"] = True
    return JSONResponse({"ok": True, "down": True})


async def admin_up(request: Request) -> JSONResponse:
    STATE["down"] = False
    return JSONResponse({"ok": True, "down": False})


async def health(request: Request) -> JSONResponse:
    if STATE["down"]:
        return JSONResponse({"status": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "engine": ENGINE})


def build_app() -> Starlette:
    STATE["down"] = False
    return Starlette(routes=[
        Route("/check", check, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/admin/down", admin_down, methods=["POST"]),
        Route("/admin/up", admin_up, methods=["POST"]),
    ])
