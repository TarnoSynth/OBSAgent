"""Budowa instancji BaseProvider z config.yaml i zmiennych środowiska."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .base import BaseProvider
from .openai import OpenAIProvider


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

    raise NotImplementedError(f"Provider '{name}' nie jest jeszcze zaimplementowany w fabryce")
