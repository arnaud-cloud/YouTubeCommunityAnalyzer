"""
Unified LLM client abstraction supporting Anthropic and Ollama backends.

Reads configuration from the settings table via a dict interface so it can
be used from both the web layer and headless scripts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def _settings_to_llm_config(settings: dict[str, str]) -> dict:
    """Convert flat settings dict to the nested config structure LLMClient expects."""
    return {
        "llm": {
            "ollama_base_url": settings.get("ollama_base_url", "http://localhost:11434"),
            "temperature": float(settings.get("llm_temperature", "0.2")),
            "summarize": {
                "backend": settings.get("llm_summarize_backend", "ollama"),
                "anthropic_model": settings.get("llm_summarize_anthropic_model", "claude-haiku-4-5"),
                "ollama_model": settings.get("llm_summarize_ollama_model", "mistral-nemo:12b"),
                "max_tokens": int(settings.get("llm_summarize_max_tokens", "4096")),
            },
            "analyze": {
                "backend": settings.get("llm_analyze_backend", "anthropic"),
                "anthropic_model": settings.get("llm_analyze_anthropic_model", "claude-sonnet-4-6"),
                "ollama_model": settings.get("llm_analyze_ollama_model", "mistral-nemo:12b"),
                "max_tokens": int(settings.get("llm_analyze_max_tokens", "16000")),
            },
            "discovery": {
                "backend": settings.get("llm_discovery_backend", "anthropic"),
                "anthropic_model": settings.get("llm_discovery_anthropic_model", "claude-haiku-4-5"),
                "ollama_model": settings.get("llm_discovery_ollama_model", "mistral-nemo:12b"),
                "max_tokens": 1024,
            },
            "tone": {
                "backend": settings.get("llm_tone_backend", "ollama"),
                "anthropic_model": settings.get("llm_tone_anthropic_model", "claude-haiku-4-5"),
                "ollama_model": settings.get("llm_tone_ollama_model", "mistral-nemo:12b"),
                "max_tokens": 1024,
            },
            "creator": {
                "backend": settings.get("llm_creator_backend", "anthropic"),
                "anthropic_model": settings.get("llm_creator_anthropic_model", "claude-haiku-4-5-20251001"),
                "ollama_model": settings.get("llm_creator_ollama_model", "mistral-nemo:12b"),
                "max_tokens": 1024,
            },
        },
        "_env": {
            "youtube_api_key": settings.get("youtube_api_key", ""),
            "anthropic_api_key": settings.get("anthropic_api_key", ""),
        },
    }


class LLMClient:
    """
    Unified interface for Anthropic and Ollama backends.

    Pass role="summarize" or role="analyze" to select the backend/model
    configured for that pipeline step.
    """

    def __init__(self, cfg: dict, role: str = "summarize"):
        llm_cfg = cfg["llm"]
        self.temperature = llm_cfg.get("temperature", 0.2)

        role_cfg = llm_cfg.get(role, {})
        self.backend = role_cfg.get("backend") or llm_cfg.get("backend", "anthropic")
        self.max_tokens = role_cfg.get("max_tokens") or 4096

        self.max_tokens_summarize = (
            llm_cfg.get("summarize", {}).get("max_tokens") or 4096
        )
        self.max_tokens_analyze = (
            llm_cfg.get("analyze", {}).get("max_tokens") or 16000
        )

        log.info(
            f"LLMClient role={role}: backend={self.backend}, "
            f"max_tokens={self.max_tokens}"
        )

        if self.backend == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=cfg["_env"]["anthropic_api_key"]
            )
            self._model = (
                role_cfg.get("anthropic_model")
                or llm_cfg.get("anthropic_model", "claude-sonnet-4-6")
            )
        elif self.backend == "ollama":
            import requests as _requests
            self._requests = _requests
            self._ollama_url = llm_cfg.get("ollama_base_url", "http://localhost:11434")
            self._model = (
                role_cfg.get("ollama_model")
                or llm_cfg.get("ollama_model", "llama3.1:8b")
            )
        else:
            raise ValueError(
                f"Unknown LLM backend '{self.backend}' for role '{role}'. "
                f"Expected 'anthropic' or 'ollama'."
            )

    def complete(self, system: str, user: str,
                 max_tokens: int | None = None,
                 json_mode: bool = False) -> str:
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        if self.backend == "anthropic":
            response = self._client.messages.create(
                model=self._model,
                max_tokens=tokens,
                temperature=self.temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return response.content[0].text
        elif self.backend == "ollama":
            payload = {
                "model": self._model,
                "prompt": f"<system>{system}</system>\n\n{user}",
                "stream": False,
                "options": {"temperature": self.temperature, "num_predict": tokens},
            }
            if json_mode:
                payload["format"] = "json"
            r = self._requests.post(
                f"{self._ollama_url}/api/generate", json=payload, timeout=120
            )
            r.raise_for_status()
            return r.json()["response"]

    @staticmethod
    def _repair_json(s: str) -> str:
        s = re.sub(r'"([^"\n]{0,120})\'\s*:', r'"\1":', s)
        s = re.sub(r'(:\s*)"([^"\n]{0,300})\'(\s*[,}\]])', r'\1"\2"\3', s)
        s = re.sub(r',(\s*[}\]])', r'\1', s)
        stripped = s.rstrip()
        if stripped.endswith(','):
            stripped = stripped[:-1].rstrip()
        open_chars = [c for c in stripped if c in '{[']
        close_map = {'[': ']', '{': '}'}
        tail = ''
        for c in reversed(open_chars):
            expected = close_map[c]
            n_open = stripped.count(c)
            n_close = stripped.count(expected) + tail.count(expected)
            if n_open > n_close:
                tail += expected
        return stripped + tail

    def complete_json(self, system: str, user: str,
                      max_tokens: int | None = None) -> Any:
        raw = self.complete(system, user, max_tokens=max_tokens, json_mode=True)
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        repaired = self._repair_json(cleaned)
        try:
            result = json.loads(repaired)
            log.warning("JSON required repair before parsing.")
            return result
        except json.JSONDecodeError as e:
            stripped = cleaned.rstrip()
            looks_truncated = not stripped.endswith(("}", "]}"))
            if looks_truncated:
                hint = (
                    " Output appears TRUNCATED -- try increasing max_tokens "
                    f"in settings (currently {max_tokens or self.max_tokens})."
                )
            else:
                hint = (
                    " Output contains malformed JSON -- "
                    "likely a local model quality issue."
                )
            log.error(f"JSON parse failed: {e}{hint}\nRaw output ({len(raw)} chars):\n{raw}")
            raise
