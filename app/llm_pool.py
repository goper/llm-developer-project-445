"""Бонус: пул из двух LLM-провайдеров со своей политикой переключения.

Основной провайдер — прежние LLM_BASE_URL / LLM_MODEL / LLM_API_KEY.
Пул включается, когда задан LLM_SECONDARY_BASE_URL.

Политика (без LiteLLM — механика видна в коде):
  - повторяется только то, что имеет смысл повторять: 429, 5xx, таймаут и
    сетевая ошибка отправляют вызов к следующему провайдеру; 400/401/403 —
    ошибка вызова, у другого провайдера она будет той же, поэтому сразу наверх;
  - провайдер, вернувший 429 / 5xx / таймаут, исключается из пула на
    LLM_FAILOVER_COOLDOWN_SEC секунд — контур не долбит упавший сервер;
  - пул обходится один раз.

Пул встроен на уровне клиента: запуск агента получает RunConfig с
model_provider и model фактического провайдера. Агенты собираются как
раньше (build_agent) и о пуле не знают — при переключении их не пересобираем.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from agents import RunConfig, Runner
from agents.models.openai_provider import OpenAIProvider
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class Provider:
    name: str
    model: str
    client: AsyncOpenAI
    price_rub_per_1m: float

    @property
    def run_config(self) -> RunConfig:
        return RunConfig(
            model=self.model,
            model_provider=OpenAIProvider(openai_client=self.client, use_responses=False),
            tracing_disabled=True,
        )


class AllProvidersFailed(RuntimeError):
    pass


class LlmPool:
    def __init__(self, providers: list[Provider], cooldown_sec: float) -> None:
        self.providers = providers
        self.cooldown_sec = cooldown_sec
        self.banned_until: dict[str, float] = {}

    def available(self) -> list[Provider]:
        now = time.monotonic()
        return [p for p in self.providers if self.banned_until.get(p.name, 0) <= now]

    def ban(self, provider: Provider) -> None:
        self.banned_until[provider.name] = time.monotonic() + self.cooldown_sec

    @staticmethod
    def retryable(error: Exception) -> bool:
        if isinstance(error, APIStatusError):
            return error.status_code in RETRYABLE_STATUS
        return isinstance(error, (APITimeoutError, APIConnectionError, httpx.TimeoutException, httpx.TransportError))

    async def call(self, fn):
        """fn(provider) → результат; пул обходится один раз."""
        last_error: Exception | None = None
        candidates = self.available() or list(self.providers)  # все на паузе — пробуем всё равно
        for provider in candidates:
            try:
                return provider, await fn(provider)
            except Exception as e:  # noqa: BLE001 — классифицируем ниже
                if not self.retryable(e):
                    raise  # 400/401/403 и прочие ошибки вызова — не переключаем
                last_error = e
                self.ban(provider)
        raise AllProvidersFailed("все провайдеры недоступны") from last_error


_POOL: LlmPool | None = None


def _client(base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "none",
        http_client=httpx.AsyncClient(trust_env=False, timeout=180),
        max_retries=0,  # повторы решает политика пула, а не клиент
    )


def setup_pool() -> LlmPool | None:
    """Собрать пул из .env; без LLM_SECONDARY_BASE_URL пул выключен."""
    global _POOL
    secondary_url = os.environ.get("LLM_SECONDARY_BASE_URL")
    if not secondary_url:
        _POOL = None
        return None
    primary = Provider(
        name="primary",
        model=os.environ["LLM_MODEL"],
        client=_client(os.environ["LLM_BASE_URL"], os.environ.get("LLM_API_KEY", "none")),
        price_rub_per_1m=float(os.environ.get("LLM_PRICE_RUB_PER_1M", "60")),
    )
    secondary = Provider(
        name="secondary",
        model=os.environ.get("LLM_SECONDARY_MODEL", primary.model),
        client=_client(secondary_url, os.environ.get("LLM_SECONDARY_API_KEY", "none")),
        price_rub_per_1m=float(os.environ.get("LLM_SECONDARY_PRICE_RUB_PER_1M", "0")),
    )
    _POOL = LlmPool([primary, secondary], float(os.environ.get("LLM_FAILOVER_COOLDOWN_SEC", "60")))
    return _POOL


def enabled() -> bool:
    return _POOL is not None


async def run_with_failover(agent, task, *, max_turns: int = 8):
    """Runner.run через пул → (фактический провайдер, RunResult)."""
    if _POOL is None:
        raise RuntimeError("пул не настроен: задайте LLM_SECONDARY_BASE_URL")
    return await _POOL.call(lambda p: Runner.run(agent, task, max_turns=max_turns, run_config=p.run_config))
