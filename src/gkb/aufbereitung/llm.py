"""Austauschbare LLM-Anbindung. Alle Aufrufe liefern JSON, das gegen ein Pydantic-Modell geprüft wird.

Wichtig: An das LLM gehen nur pseudonymisierte Inhalte. Dateipfade im Klartext werden nicht gesendet.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def schaetze_tokens(text: str) -> int:
    """Grobe Schätzung (ca. 4 Zeichen je Token). Für Budgets und das Feld details.tokens."""
    return max(1, len(text) // 4)


class LLM(Protocol):
    name: str

    def json(self, system: str, user: str) -> dict: ...


LOKALE_ANBIETER = ("hosted_vllm/", "ollama/", "ollama_chat/", "openai/")


class LiteLLM:
    """Anbindung über LiteLLM (Extra 'llm').

    Cloud:  'anthropic/<modell>' (API-Schlüssel nach LiteLLM-Konvention, z. B. ANTHROPIC_API_KEY)
    Lokal:  'hosted_vllm/gkb-lokal' mit api_base 'http://llm:8000/v1' (Dienst "llm" in docker-compose.gpu.yml)
    """

    def __init__(self, model: str, max_tokens: int = 2000, api_base: str | None = None):
        import litellm  # noqa: F401  (Fehler früh, falls Extra fehlt)
        self.name = model
        self.max_tokens = max_tokens
        self.api_base = api_base or None
        self.lokal = model.startswith(LOKALE_ANBIETER)

    def json(self, system: str, user: str) -> dict:
        import litellm
        extra = {}
        if self.api_base:
            extra["api_base"] = self.api_base
        if self.lokal:
            # vLLM/Ollama erzwingen damit syntaktisch gültiges JSON (hilft kleineren Modellen sehr)
            extra["response_format"] = {"type": "json_object"}
        r = litellm.completion(
            model=self.name,
            max_tokens=self.max_tokens,
            temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        )
        return _parse_json(r.choices[0].message.content)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def frage(llm: LLM, system: str, user: str, model: type[T], versuche: int = 2) -> T:
    """LLM fragen und gegen das Schema prüfen. Bei Fehlern einmal mit Fehlerhinweis wiederholen."""
    hinweis = ""
    letzter: Exception | None = None
    for _ in range(versuche):
        try:
            return model.model_validate(llm.json(system, user + hinweis))
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            letzter = e
            hinweis = f"\n\nDeine letzte Antwort war ungültig ({str(e)[:500]}). Antworte nur mit gültigem JSON."
            log.warning("Ungültige LLM-Antwort (%s), neuer Versuch", type(e).__name__)
    raise ValueError(f"LLM-Antwort nach {versuche} Versuchen ungültig: {letzter}")
