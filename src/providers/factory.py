"""Budowa instancji BaseProvider z config.yaml i zmiennych środowiska."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .base import BaseProvider
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from .openrouter import OpenRouterProvider


def _project_root() -> Path:
    """Katalog główny repozytorium (nad ``src``)."""
    return Path(__file__).resolve().parents[2]


def load_config_dict(path: Path | str) -> dict[str, Any]:
    """Wczytuje YAML do słownika."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config YAML musi mieć na górze mapę (klucz: wartość)")
    return raw


def build_provider(config_path: Path | str | None = None) -> BaseProvider:
    """
    Czyta ``provider`` i ``providers.<nazwa>.model`` z YAML.
    Klucze API z ``.env`` (po ``load_dotenv()``).
    """
    load_dotenv()
    path = Path(config_path) if config_path else _project_root() / "config.yaml"
    cfg = load_config_dict(path)

    name = cfg.get("provider")
    if not name or not isinstance(name, str):
        raise ValueError("config: pole 'provider' (string) jest wymagane")

    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("config: sekcja 'providers' musi być mapą")

    if name == "openai":
        section = providers.get("openai")
        if not isinstance(section, dict):
            raise ValueError("config: brak providers.openai")
        model = section.get("model")
        if not model:
            raise ValueError("config: providers.openai.model jest wymagane")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Brak OPENAI_API_KEY — ustaw w .env lub środowisku")
        return OpenAIProvider(api_key=api_key, default_model=str(model))

    if name == "anthropic":
        section = providers.get("anthropic")
        if not isinstance(section, dict):
            raise ValueError("config: brak providers.anthropic")
        model = section.get("model")
        if not model:
            raise ValueError("config: providers.anthropic.model jest wymagane")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("Brak ANTHROPIC_API_KEY — ustaw w .env lub środowisku")

        default_extra: dict[str, Any] = {}
        effort = section.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or effort not in {
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            }:
                raise ValueError(
                    "config: providers.anthropic.effort musi byc jednym z: "
                    "low, medium, high, xhigh, max"
                )
            default_extra["output_config"] = {"effort": effort}

        thinking = section.get("thinking")
        if thinking is not None:
            if not isinstance(thinking, dict):
                raise ValueError(
                    "config: providers.anthropic.thinking musi byc mapa "
                    "(np. {type: enabled, budget_tokens: 16000})"
                )
            if effort is not None:
                raise ValueError(
                    "config: ustaw ALBO providers.anthropic.effort ALBO thinking, "
                    "ale nie oba naraz (effort zastepuje deprecated budget_tokens "
                    "dla Opus 4.6+/Sonnet 4.6+, a na Opus 4.7 thinking jest juz odrzucane)"
                )
            default_extra["thinking"] = dict(thinking)

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "default_model": str(model),
        }
        max_tokens_raw = section.get("max_tokens")
        if max_tokens_raw is not None:
            try:
                kwargs["default_max_tokens"] = int(max_tokens_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "config: providers.anthropic.max_tokens musi byc liczba calkowita"
                ) from exc
        if default_extra:
            kwargs["default_extra"] = default_extra

        return AnthropicProvider(**kwargs)

    if name == "openrouter":
        section = providers.get("openrouter")
        if not isinstance(section, dict):
            raise ValueError("config: brak providers.openrouter")
        model = section.get("model")
        if not model:
            raise ValueError("config: providers.openrouter.model jest wymagane")

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Brak OPENROUTER_API_KEY — ustaw w .env lub środowisku")

        base_url = section.get("base_url")
        http_referer = section.get("http_referer") or os.environ.get("OPENROUTER_HTTP_REFERER")
        app_title = section.get("title") or os.environ.get("OPENROUTER_TITLE")
        timeout_raw = section.get("timeout", 60.0)

        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("config: providers.openrouter.timeout musi byc liczba") from exc

        return OpenRouterProvider(
            api_key=api_key,
            default_model=str(model),
            base_url=str(base_url) if base_url else "https://openrouter.ai/api/v1",
            http_referer=str(http_referer) if http_referer else None,
            app_title=str(app_title) if app_title else None,
            timeout=timeout,
        )

    raise NotImplementedError(f"Provider '{name}' nie jest jeszcze zaimplementowany w fabryce")
