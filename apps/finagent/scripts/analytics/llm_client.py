# -*- coding: utf-8 -*-
"""Pluggable LLM client for analytics jobs.

The default production provider is OpenAI-compatible. Bailian, DeepSeek, OpenAI,
and most compatible gateways can be selected by changing environment variables:

  LLM_PROVIDER=openai_compatible
  LLM_BASE_URL=https://example.com/v1
  LLM_API_KEY_ENV=SUB2API_API_KEY  # optional; defaults to LLM_API_KEY
  LLM_MODEL_FAST=...
  LLM_MODEL_REASONING=...
"""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


def strip_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    return json.loads(text)


@dataclass
class LLMConfig:
    provider: str
    base_url: str | None
    api_key: str | None
    model_fast: str
    model_reasoning: str
    timeout: int = 60
    api_mode: str = "chat_completions"
    reasoning_effort: str = ""
    retries: int = 3

    @classmethod
    def from_env(cls) -> "LLMConfig":
        model_fast = os.getenv("LLM_MODEL_FAST") or os.getenv("BAILIAN_MODEL_FAST") or "qwen3.6-flash"
        model_reasoning = os.getenv("LLM_MODEL_REASONING") or os.getenv("BAILIAN_MODEL_PLUS") or model_fast
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai_compatible"),
            base_url=os.getenv("LLM_BASE_URL") or os.getenv("BAILIAN_BASE_URL"),
            api_key=(
                os.getenv(os.getenv("LLM_API_KEY_ENV", "LLM_API_KEY"))
                or os.getenv("BAILIAN_API_KEY")
            ),
            model_fast=model_fast,
            model_reasoning=model_reasoning,
            timeout=int(os.getenv("LLM_TIMEOUT", "60")),
            api_mode=(os.getenv("LLM_API_MODE") or os.getenv("FINANCE_API_MODE") or "chat_completions").strip().lower(),
            reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip(),
            retries=int(os.getenv("LLM_RETRIES", "3")),
        )


class BaseLLMClient:
    def chat_json(self, messages: list[dict[str, str]], schema_name: str, model: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(self, config: LLMConfig):
        self.config = config
        if not config.base_url:
            raise LLMError("LLM_BASE_URL is not set.")
        if not config.api_key:
            raise LLMError("LLM_API_KEY is not set.")

    def chat_json(self, messages: list[dict[str, str]], schema_name: str, model: str | None = None) -> dict[str, Any]:
        selected_model = model or self.config.model_fast
        responses_mode = self.config.api_mode in {"responses", "response"}
        url = self.config.base_url.rstrip("/") + ("/responses" if responses_mode else "/chat/completions")
        if responses_mode:
            input_text = "\n\n".join(
                f"[{str(message.get('role') or 'user').upper()}]\n{message.get('content') or ''}"
                for message in messages
            ) + "\n\n[OUTPUT CONTRACT]\nReturn exactly one valid JSON object and no markdown fences."
            request_body: dict[str, Any] = {"model": selected_model, "input": input_text}
            if self.config.reasoning_effort:
                request_body["reasoning"] = {"effort": self.config.reasoning_effort}
        else:
            request_body = {
                "model": selected_model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1800,
            }

        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        resp = None
        for attempt in range(max(0, self.config.retries) + 1):
            resp = requests.post(
                url,
                headers=headers,
                json=request_body,
                timeout=self.config.timeout,
            )
            if resp.status_code in {400, 422} and "reasoning" in request_body:
                request_body.pop("reasoning", None)
            elif resp.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                break
            if attempt < self.config.retries:
                time.sleep(min(5 * (2 ** attempt), 30))
        assert resp is not None
        resp.raise_for_status()
        payload = resp.json()
        if responses_mode:
            content = "".join(
                part.get("text", "")
                for item in payload.get("output") or []
                if item.get("type") == "message"
                for part in item.get("content") or []
                if part.get("type") in {"output_text", "text"}
            )
        else:
            content = payload["choices"][0]["message"]["content"]
        return {
            "provider": self.config.provider,
            "model": selected_model,
            "schema_name": schema_name,
            "raw_response": payload,
            "parsed": strip_json(content),
        }


class MockLLMClient(BaseLLMClient):
    """Deterministic fallback for smoke tests and offline development."""

    def chat_json(self, messages: list[dict[str, str]], schema_name: str, model: str | None = None) -> dict[str, Any]:
        user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        parsed = {
            "concepts": [],
            "edges": [],
            "company_relations": [],
            "confidence": 0.5,
        }
        if "液冷" in user_text:
            parsed["concepts"].append(
                {
                    "name": "液冷",
                    "normalized_name": "液冷",
                    "concept_type": "component",
                    "parent_name": "散热",
                    "relation_type": "contains",
                    "relevance_score": 0.9,
                    "reason": "AI服务器功耗提升带来散热升级。",
                }
            )
        return {"provider": "mock", "model": model or "mock", "schema_name": schema_name, "raw_response": parsed, "parsed": parsed}


def get_llm_client(provider: str | None = None) -> BaseLLMClient:
    config = LLMConfig.from_env()
    selected = provider or config.provider
    if selected == "mock":
        return MockLLMClient()
    if selected == "openai_compatible":
        return OpenAICompatibleClient(config)
    raise LLMError(f"Unsupported LLM_PROVIDER={selected}")
