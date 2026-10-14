"""
OpenRouter для MiroFish — наш модуль, кладётся в app/utils/ форка.

Форк умеет только claude-cli и codex-cli. Этот модуль добавляет третий путь:
любой OpenAI-совместимый endpoint, а практически — OpenRouter, за которым
живут Qwen, Kimi, DeepSeek, GPT и Claude с общим биллингом.

Главное, ради чего всё затевалось: OASIS принимает СПИСОК моделей и раздаёт их
агентам по кругу. Поэтому «совет разных ИИ» собирается конфигом, без правки
движка: перечислил модели в LLM_MODELS — часть толпы думает одной, часть другой.

Чужой AGPL-код мы не трогаем: здесь только наш модуль, врезки в три файла
форка делает apply.py и они идемпотентны.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Набор по умолчанию: разные вендоры, разный «характер» рассуждений — именно это
# и даёт пестроту мнений. Дешёвые модели намеренно впереди: на них приходится
# основная масса рутинных действий агентов.
# Идентификаторы сверены с /api/v1/models 11.10.2026 — старый
# `anthropic/claude-3.5-haiku` отдаёт 404 «No endpoints found».
DEFAULT_MIX = [
    "qwen/qwen-plus",
    "deepseek/deepseek-chat",
    "moonshotai/kimi-k2",
    "anthropic/claude-haiku-5.5",
]


def is_openrouter(provider: str | None) -> bool:
    return (provider or "").lower() in ("openrouter", "openai-compatible")


def configured_models(config: Dict[str, Any] | None = None) -> List[str]:
    """Список моделей: из конфига прогона, из LLM_MODELS или набор по умолчанию."""
    if config:
        from_config = config.get("llm_models")
        if isinstance(from_config, list) and from_config:
            return [str(m).strip() for m in from_config if str(m).strip()]

    raw = os.environ.get("LLM_MODELS", "").strip()
    if raw:
        models = [m.strip() for m in raw.replace("\n", ",").split(",") if m.strip()]
        if models:
            return models

    single = os.environ.get("LLM_MODEL_NAME", "").strip()
    if single:
        return [single]
    return list(DEFAULT_MIX)


def _api_key() -> str:
    key = (os.environ.get("LLM_API_KEY")
           or os.environ.get("OPENROUTER_API_KEY")
           or "").strip()
    if not key:
        raise RuntimeError(
            "Нет ключа OpenRouter: задай OPENROUTER_API_KEY (или LLM_API_KEY) "
            "в .env движка."
        )
    return key


def _base_url() -> str:
    return (os.environ.get("LLM_BASE_URL") or OPENROUTER_URL).strip()


def build_models(config: Dict[str, Any] | None = None, use_boost: bool = False):
    """
    Возвращает модель или список моделей для OASIS.

    Список OASIS раздаёт агентам по кругу — так разные персоны получают
    разных «мыслителей». Одна модель — обычный однородный прогон.
    """
    from camel.models.openai_model import OpenAIModel

    models = configured_models(config)
    if use_boost:
        boost = os.environ.get("LLM_BOOST_MODEL_NAME", "").strip()
        if boost:
            models = [boost]

    key, url = _api_key(), _base_url()
    built = []
    for name in models:
        built.append(OpenAIModel(
            model_type=name,
            model_config_dict={"temperature": 0.8, "max_tokens": 2048},
            api_key=key,
            url=url,
        ))
    # Видно в simulation.log: какие модели реально раздали агентам.
    print(f"[mirofish-ops] openrouter models={models} boost={use_boost}", flush=True)
    return built[0] if len(built) == 1 else built


def chat_completion(messages: List[Dict[str, str]], *, model: str | None = None,
                    temperature: float = 0.7, max_tokens: int = 4096,
                    json_mode: bool = False, timeout: int = 300) -> str:
    """Прямой вызов для конвейера (онтология, граф, отчёт) — вне OASIS."""
    from openai import OpenAI

    client = OpenAI(api_key=_api_key(), base_url=_base_url(), timeout=timeout)
    target = model or configured_models()[0]

    kwargs: Dict[str, Any] = {
        "model": target,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # OpenRouter показывает это в своей консоли — удобно разбирать расход.
        "extra_headers": {
            "HTTP-Referer": os.environ.get("MIROFISH_PUBLIC_URL", "https://localhost/ai"),
            "X-Title": os.environ.get("MIROFISH_APP_TITLE", "MiroFish"),
        },
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)
    # Какая модель ответила на самом деле: OpenRouter может подменить вариант
    # провайдера, и без этой строки не доказать, что считала именно она.
    print(f"[mirofish-ops] call model={target} served_by={getattr(response, 'model', '?')}",
          flush=True)
    if not response.choices:
        raise RuntimeError(f"{target}: пустой ответ")
    return (response.choices[0].message.content or "").strip()
