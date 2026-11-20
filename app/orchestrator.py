"""Оркестратор хода: guardrail → карточка → план → гейт → агенты → редактор → SSE.

Ход — одна реплика абонента. Модель только предлагает: план проверяет гейт,
ответ на карточку распознаёт код (cards.classify_answer), исполнение вызывает
код после ConfirmCard и Execute политики, повтор отсекает ключ идемпотентности.
Каждый исход пути действия ложится в аудит с trace_id хода.

События SSE: plan / card / reply / error (отправляет main) / done.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from app import audit, cards, editor, guardrail_client, idempotency, llm, planner
from app.sessions import Session, pep_of
from app.workers import build_workers

Emit = Callable[[dict], Awaitable[None]]

ACTION_TITLES = {
    "tariff_change": "смена тарифа",
    "service_toggle": "изменение услуги",
    "contacts_update": "смена контактов",
}

REFUSE_INPUT = "Я не могу выполнить такую просьбу. Спросите о тарифе, списаниях или услугах — помогу."
REFUSE_OUTPUT = "Не могу показать этот ответ. Могу позвать оператора, если нужно."
GUARD_DOWN_ACTIONS = "Сейчас не работает проверка безопасности, поэтому изменения недоступны. Могу позвать оператора."

HUMAN_REASONS = {
    "session_limit": "За этот разговор тариф уже меняли дважды — третья смена недоступна. Могу позвать оператора.",
    "confirmation_expired": "Карточка подтверждения устарела. Повторите запрос — я подготовлю новую.",
    "card_not_prepared": "Не нашёл, что подтверждать. Опишите, что хотите изменить.",
    "confirmation_missing": "Подтверждение не найдено. Повторите запрос — я подготовлю новую карточку.",
    "confirmation_mismatch": "Подтверждение относится к другой карточке. Повторите запрос.",
    "pdp_unavailable": "Сервис проверки прав сейчас не отвечает, поэтому изменения недоступны. Могу позвать оператора.",
    "guard_not_ok": GUARD_DOWN_ACTIONS,
    "no_write_scope": "Изменения вам недоступны. Могу позвать оператора.",
    "role_has_no_write": "Эта роль не выполняет изменения.",
}

DUPLICATE_TEXT = "Эта операция уже исполнена — повтор не выполнялся."


def _new_trace(session: Session) -> str:
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    session.turn_sources = []
    session.new_card = None
    return session.trace_id


def _audit(session: Session, what: str, outcome: str, **kw) -> None:
    audit.append(session.user.sub, what, outcome, trace_id=session.trace_id, session_id=session.session_id, **kw)


async def check_input(session: Session, message: str) -> str | None:
    """Проверка реплики до LLM. Возвращает текст отказа или None (идём дальше)."""
    verdict = await guardrail_client.check(message, "input")
    session.guard_state["ok"] = verdict.available
    if not verdict.available:
        _audit(session, "guardrail", "guardrail_degraded", detail="input: guardrail недоступен")
        return None
    if not verdict.safe:
        _audit(session, "guardrail", "guardrail_unsafe", detail=f"input:{verdict.category}")
        return REFUSE_INPUT
    return None


async def check_output(session: Session, text: str) -> bool:
    """Проверка ответа/карточки до показа. True — можно показывать."""
    verdict = await guardrail_client.check(text, "output")
    if not verdict.available:
        session.guard_state["ok"] = False
        _audit(session, "guardrail", "guardrail_degraded", detail="output: guardrail недоступен")
        return True  # чтение продолжается; действия закроет политика
    if not verdict.safe:
        _audit(session, "guardrail", "guardrail_unsafe", detail=f"output:{verdict.category}")
        return False
    return True


# ── путь действия ────────────────────────────────────────────────────────

def _done_text(card) -> str:
    return f"Готово, операция «{ACTION_TITLES.get(card.action, card.action)}» выполнена."


async def execute_pending(session: Session, card) -> str:
    """«Да» по карточке: ConfirmCard → Execute → идемпотентное исполнение → аудит."""
    key = f"{card.action}:{card.pending_id}"
    if idempotency.lookup(key) is not None:  # повторное «да» по исполненной карточке
        _audit(session, card.action, "duplicate_suppressed", idempotency_key=key)
        return DUPLICATE_TEXT

    pep = pep_of(session)
    for ask in (pep.confirm, pep.execute):
        decision = await ask(card)
        if not decision.allowed:
            state = "expired" if decision.reason == "confirmation_expired" else "denied"
            cards.settle(session, card, state)
            _audit(session, card.action, "expired" if state == "expired" else "denied",
                   args=card.details, detail=decision.reason)
            return HUMAN_REASONS.get(decision.reason, "Это действие сейчас недоступно. Могу позвать оператора.")

    cards.settle(session, card, "confirmed")

    async def run_execute() -> dict:
        try:
            return await session.pool.call("actions", "execute_action", {
                "action": card.action,
                "details_json": json.dumps(card.details, ensure_ascii=False),
                "pending_id": card.pending_id,
                "idempotency_key": key,
            })
        except Exception as e:  # noqa: BLE001 — сбой исполнения становится исходом
            data = getattr(e, "data", None)
            return {"error": (data or {}).get("error", "execute_failed") if isinstance(data, dict) else "execute_failed",
                    "detail": str(e)[:200]}

    stored = await idempotency.execute(key, card.sub, card.action, run_execute)
    result = stored.result
    outcome = result.get("outcome")
    session.last_card = card

    if stored.duplicate or outcome == "duplicate":
        _audit(session, card.action, "duplicate_suppressed", idempotency_key=key)
        return DUPLICATE_TEXT
    if outcome == "daily_limit":
        _audit(session, card.action, "server_limit", idempotency_key=key, detail="daily_limit")
        return "Сегодня тариф уже меняли максимальное число раз — биллинг не разрешает больше. Попробуйте завтра."
    if outcome == "executed":
        _audit(session, card.action, "executed", args=card.details, before=result.get("before"),
               after=result.get("after"), idempotency_key=key)
        return _done_text(card)
    _audit(session, card.action, "error", args=card.details, idempotency_key=key, detail=_short(result))
    return "Не получилось выполнить операцию. Могу позвать оператора."


def _short(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:200]


async def reject_pending(session: Session, card) -> str:
    cards.settle(session, card, "rejected")
    _audit(session, card.action, "rejected_by_user", args=card.details)
    return "Хорошо, отменяю. Ничего не изменилось."


async def escalate(session: Session, card=None, note: str = "") -> str:
    """Позвать человека: тикет создаёт код, в аудит — escalated."""
    if card is not None:
        cards.settle(session, card, "rejected")
    subject = f"Абонент просит оператора: {ACTION_TITLES.get(card.action, card.action)}" if card else "Абонент просит оператора"
    try:
        result = await session.pool.call("actions", "create_ticket", {"subject": subject, "note": note[:300]})
    except Exception as e:  # noqa: BLE001
        _audit(session, "create_ticket", "error", detail=str(e)[:200])
        return "Не получилось создать обращение. Попробуйте позже."
    _audit(session, "create_ticket", "escalated", detail=str(result.get("ticket_id")))
    return f"Создал обращение {result.get('ticket_id')}, оператор свяжется с вами."


# ── ход ──────────────────────────────────────────────────────────────────

async def run_turn(session: Session, message: str, emit: Emit) -> None:
    _new_trace(session)

    refusal = await check_input(session, message)
    if refusal:
        await emit({"type": "reply", "text": refusal})
        return

    # живая карточка: ответ на неё разбирает код, а не модель
    card = cards.active(session)
    answer = cards.classify_answer(message)
    if card is not None:
        if answer == "yes":
            await emit({"type": "reply", "text": await execute_pending(session, card)})
            return
        if answer == "no":
            await emit({"type": "reply", "text": await reject_pending(session, card)})
            return
        if answer == "operator":
            await emit({"type": "reply", "text": await escalate(session, card)})
            return
        # topic: смена темы — обычный ход, карточка остаётся живой
    elif answer == "yes" and _is_bare_answer(message):
        last = session.last_card
        if last is not None and idempotency.lookup(f"{last.action}:{last.pending_id}") is not None:
            _audit(session, last.action, "duplicate_suppressed", idempotency_key=f"{last.action}:{last.pending_id}")
            await emit({"type": "reply", "text": DUPLICATE_TEXT})
        else:
            _audit(session, "confirm", "denied", detail="card_not_prepared")
            await emit({"type": "reply", "text": HUMAN_REASONS["card_not_prepared"]})
        return

    # план → гейт
    if session.planner is None:
        session.planner = planner.build_planner()
    plan, session.thread = await planner.next_plan(session.planner, session.thread, message, session.trace_id)
    plan, dropped = planner.gate(plan)
    session.trace.extend(dropped)
    await emit({"type": "plan", "agents": [s.agent for s in plan.steps], "dropped": dropped})

    if not plan.steps:
        text = plan.question or "Уточните, пожалуйста, что вы хотите узнать или изменить?"
        await emit({"type": "reply", "text": text})
        return

    # доменные агенты: каждый шаг — отдельный запуск
    workers = build_workers(session, tuple(s.agent for s in plan.steps))
    runs = []
    for step in plan.steps:
        agent = workers.get(step.agent)
        if agent is None:
            continue
        task = step.task or message
        if step.agent in ("tariff", "tickets"):
            task = f"Просьба абонента: «{message}». Задача: {task}"
        run = await llm.run_agent(agent, task, trace_id=session.trace_id)
        runs.append({"agent": step.agent, **run})
        if session.new_card is not None:
            break  # карточка готова — ждём подтверждения

    # свежая карточка: проверяем текст и показываем, ход завершается
    if session.new_card is not None:
        card = session.new_card
        report = next((r["report"] for r in runs if r["agent"] == "tariff"), "")
        card_text = " ".join(f"{k}: {v}" for k, v in card.view.items())
        if not await check_output(session, f"{report}\n{card_text}"):
            cards.settle(session, card, "denied")
            await emit({"type": "reply", "text": REFUSE_OUTPUT})
            return
        if not session.guard_state.get("ok", True):
            cards.settle(session, card, "denied")
            await emit({"type": "reply", "text": GUARD_DOWN_ACTIONS})
            return
        from core import config

        await emit({"type": "card", "pending_id": card.pending_id, "card": card.public_view(config.CARD_TTL_SEC)})
        await emit({"type": "reply", "text": "Проверьте карточку и подтвердите: ответьте «да» или «нет»."})
        return

    # редактор — единственный голос для абонента
    answer_run = await llm.run_agent(
        editor.build_editor(session),
        editor.editor_task(message, runs, name=session.user.name),
        trace_id=session.trace_id,
    )
    text = answer_run["report"] or "Не удалось подготовить ответ. Попробуйте переформулировать."
    if not await check_output(session, text):
        await emit({"type": "reply", "text": REFUSE_OUTPUT})
        return
    await emit({"type": "reply", "text": text, "sources": list(session.turn_sources)})


def _is_bare_answer(message: str) -> bool:
    """Короткий ответ вида «да», «ага, давай» — без новой просьбы."""
    return len(message.strip().split()) <= 3


async def decide_card(session: Session, pending_id: str, answer: str) -> dict:
    """Кнопка карточки: POST /api/cards/{pending_id}/decision."""
    _new_trace(session)
    verdict = await guardrail_client.check("подтверждение карточки", "input")
    session.guard_state["ok"] = verdict.available
    if not verdict.available:
        _audit(session, "guardrail", "guardrail_degraded", detail="decision: guardrail недоступен")

    card = cards.active(session)
    if card is None or card.pending_id != pending_id:
        last = session.last_card
        if answer == "yes" and last is not None and last.pending_id == pending_id:
            return {"outcome": "duplicate", "text": await execute_pending(session, last)}
        return {"outcome": "not_found", "text": "Карточка не найдена или истекла."}
    if answer == "yes":
        return {"outcome": "confirmed", "text": await execute_pending(session, card)}
    return {"outcome": "rejected", "text": await reject_pending(session, card)}
