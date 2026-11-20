"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов и снимает пометку
действия у агентов чтения. Действия (смена тарифа, услуги, контакты, тикет)
идут через карточку подтверждения и политику прав, а не напрямую.
"""
from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

READ_AGENTS = ("data", "kb")
ACTION_AGENTS = ("tariff", "tickets")
KNOWN_AGENTS = READ_AGENTS + ACTION_AGENTS
HISTORY_LIMIT = 16


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    agent: str = Field(validation_alias=AliasChoices("agent", "agentName", "agent_name", "name"))
    task: str = Field(default="", validation_alias=AliasChoices("task", "job", "goal"))
    why: str = ""
    action: bool = False

    @field_validator("action", mode="before")
    @classmethod
    def _bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "да")
        return bool(v)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    steps: list[Step] = []
    question: str | None = None


PLANNER_PROMPT = """Ты планировщик чата мобильного оператора связи. Сам ничего не делаешь и не отвечаешь абоненту:
ты только решаешь, каких агентов запустить и с какой задачей.

Агенты:
- data — данные абонента: профиль, текущий тариф, каталог тарифов, списания (только чтение).
- kb — база знаний оператора: правила списаний, смены тарифа, услуг, роуминга, описания тарифов (только чтение).
- tariff — изменения: смена тарифа, подключение/отключение услуги, смена телефона или почты. Готовит карточку подтверждения.
- tickets — позвать живого оператора / создать обращение.

Правила:
- вопрос о данных абонента (тариф, баланс, услуги) — один шаг data;
- вопрос о правилах и последствиях — шаг kb;
- «почему списание», «за что деньги» — оба: data и kb;
- просьба что-то изменить — шаг tariff с action=true;
- просьба позвать человека или вопрос вне тем оператора — шаг tickets с action=true;
- названия тарифов и услуг пиши строчными без кавычек: «на семейный», «отключить музыку»;
- в task пиши конкретную задачу агенту по-русски;
- если просьба непонятна и данных для плана мало — steps пустой, в question уточняющий вопрос.

Ответ — только JSON: {"steps": [{"agent": "...", "task": "...", "why": "...", "action": false}], "question": null}"""


def build_planner():
    from app.llm import build_agent

    return build_agent("planner", PLANNER_PROMPT, output_type=Plan)


def gate(plan: Plan) -> tuple[Plan, list[str]]:
    """Гейт: решает код, а не модель.

    - неизвестные агенты отбрасываются (причина — в трейс);
    - у агентов чтения пометка action снимается: действий там нет по построению;
    - повторные шаги одного агента склеиваются в один.
    """
    kept: list[Step] = []
    dropped: list[str] = []
    for step in plan.steps:
        name = (step.agent or "").strip().lower()
        if name not in KNOWN_AGENTS:
            dropped.append(f"gate: неизвестный агент «{step.agent}»")
            continue
        action = bool(step.action) and name in ACTION_AGENTS
        if step.action and not action:
            dropped.append(f"gate: снята пометка action у агента чтения «{name}»")
        if any(k.agent == name for k in kept):
            dropped.append(f"gate: повторный шаг агента «{name}» склеен")
            continue
        kept.append(Step(agent=name, task=step.task or "", why=step.why, action=action))
    question = plan.question if not kept else None
    return Plan(steps=kept, question=question), dropped


def _fallback(message: str) -> Plan:
    """Модель не дала разбираемый план — безопасный план только на чтение."""
    return Plan(steps=[Step(agent="data", task=message), Step(agent="kb", task=message)])


async def next_plan(planner, history: list, message: str, trace_id: str = "") -> tuple[Plan, list]:
    """Один вызов планировщика по истории диалога → (план, новая история)."""
    from app import llm

    items = list(history[-HISTORY_LIMIT:]) + [{"role": "user", "content": message}]
    try:
        result = await llm.run(planner, items, trace_id=trace_id)
    except Exception:  # noqa: BLE001 — сломанный JSON плана не роняет ход
        return _fallback(message), items
    plan = result.final_output
    if not isinstance(plan, Plan):
        try:
            plan = Plan.model_validate(plan if isinstance(plan, dict) else {})
        except Exception:  # noqa: BLE001
            plan = _fallback(message)
    return plan, result.to_input_list()[-HISTORY_LIMIT:]
