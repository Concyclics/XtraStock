"""Small secret-safe OpenAI-compatible client for scheduled report jobs."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _provider_error(error: Any) -> str:
    if isinstance(error, dict):
        kind = str(error.get("type") or error.get("code") or "provider_error")
        message = str(error.get("message") or "unknown provider error")
        return f"provider stream error [{kind}]: {message[:800]}"
    return f"provider stream error: {str(error)[:800]}"


def _responses_text_input(messages: list[dict[str, str]], json_object: bool) -> str:
    parts = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        parts.append(f"[{role}]\n{message.get('content') or ''}")
    if json_object:
        parts.append(
            "[OUTPUT CONTRACT]\nReturn exactly one valid JSON object and no markdown fences or text outside it."
        )
    return "\n\n".join(parts)


def complete(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    api_key_env: str,
    reasoning_effort: str = "high",
    timeout: int = 720,
    json_object: bool = False,
    stream: bool = False,
    retries: int = 1,
    api_mode: str | None = None,
    audit_path: Path | None = None,
) -> str:
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing model API key environment variable: {api_key_env}")
    mode = (api_mode or os.getenv("FINANCE_API_MODE", "chat_completions")).strip().lower()
    is_responses = mode in {"responses", "response"}
    url = base_url.rstrip("/") + ("/responses" if is_responses else "/chat/completions")
    payload: dict[str, Any] = {"model": model}
    if is_responses:
        payload["input"] = _responses_text_input(messages, json_object)
    else:
        payload["messages"] = messages
    if reasoning_effort:
        if is_responses:
            payload["reasoning"] = {"effort": reasoning_effort}
        else:
            payload["reasoning_effort"] = reasoning_effort
    if json_object:
        if not is_responses:
            payload["response_format"] = {"type": "json_object"}

    started = time.monotonic()
    audit: dict[str, Any] = {"requested_model": model, "reasoning_effort": reasoning_effort,
                            "api_mode": mode, "completed": False, "attempts": []}

    def save_audit() -> None:
        if audit_path is not None:
            audit["seconds"] = round(time.monotonic() - started, 3)
            Path(audit_path).write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def observe_response(body: dict[str, Any]) -> None:
        attempt = audit["attempts"][-1]
        for key in ("model", "status"):
            if body.get(key) is not None:
                attempt["response_" + key] = body[key]
        if isinstance(body.get("reasoning"), dict):
            attempt["response_reasoning_effort"] = body["reasoning"].get("effort")
        usage = body.get("usage") or {}
        if usage:
            attempt["usage"] = {key: usage[key] for key in (
                "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"
            ) if key in usage}
            attempt["usage"]["reasoning_tokens"] = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")

    def build_request(current_payload: dict[str, Any]) -> urllib.request.Request:
        audit["attempts"].append({"stream": bool(current_payload.get("stream")),
            "model": current_payload["model"],
            "reasoning_effort": (current_payload.get("reasoning") or {}).get("effort")
                                if is_responses else current_payload.get("reasoning_effort")})
        save_audit()
        return urllib.request.Request(
            url,
            data=json.dumps(current_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": (
                    "text/event-stream" if current_payload.get("stream")
                    else "application/json"
                ),
                "User-Agent": "finance-agent-v3/1.0",
            },
            method="POST",
        )

    def request_blocking(current_payload: dict[str, Any]) -> str:
        req = build_request({**current_payload, "stream": False})
        with urllib.request.urlopen(req, timeout=max(30, timeout)) as response:
            body = json.loads(response.read().decode("utf-8"))
        observe_response(body)
        if body.get("error"):
            raise RuntimeError(_provider_error(body["error"]))
        if is_responses and body.get("status") in {"incomplete", "failed", "cancelled"}:
            raise RuntimeError("model response was not completed: " + str(body["status"]))
        if is_responses:
            parts = [
                content.get("text", "")
                for item in body.get("output") or []
                if item.get("type") == "message"
                for content in item.get("content") or []
                if content.get("type") in {"output_text", "text"}
            ]
            content = "".join(parts)
        else:
            content = body["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model response did not contain textual content")
        return content.strip()

    def request_stream(current_payload: dict[str, Any]) -> str:
        req = build_request({**current_payload, "stream": True})
        content_parts: list[str] = []
        event_type = ""
        completed_response = False
        with urllib.request.urlopen(req, timeout=max(30, timeout)) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                event = json.loads(data_text)
                if event_type == "error" or event.get("error"):
                    raise RuntimeError(_provider_error(event.get("error") or event))
                if is_responses:
                    if isinstance(event.get("response"), dict):
                        observe_response(event["response"])
                    if event.get("type") == "response.output_text.delta":
                        piece = event.get("delta")
                        if isinstance(piece, str) and piece:
                            content_parts.append(piece)
                    elif event.get("type") == "response.completed":
                        completed_response = True
                    elif event.get("type") in {"response.failed", "response.incomplete", "error"}:
                        raise RuntimeError(_provider_error(event.get("response") or event))
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
        content = "".join(content_parts).strip()
        if is_responses and not completed_response:
            raise ValueError("Responses stream ended before response.completed")
        if not content:
            raise ValueError("model stream did not contain textual content")
        return content

    def http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        return RuntimeError(f"provider HTTP {exc.code}: {detail or exc.reason}")

    configured_retries = int(os.getenv("FINANCE_API_MIN_RETRIES", str(retries)))
    max_retries = max(0, retries, configured_retries)
    modes = [True, False] if stream else [False]
    last_error: Exception | None = None
    for use_stream in modes:
        current_payload = dict(payload)
        mode_retries = 0 if use_stream else max_retries
        for attempt in range(mode_retries + 1):
            try:
                if use_stream:
                    content = request_stream(current_payload)
                else:
                    content = request_blocking(current_payload)
                audit["completed"] = True
                save_audit()
                return content
            except urllib.error.HTTPError as exc:
                format_key = "text" if is_responses else "response_format"
                if exc.code == 400 and format_key in current_payload:
                    current_payload.pop(format_key, None)
                    last_error = http_error(exc)
                elif exc.code in {408, 409, 425, 429} or exc.code >= 500:
                    last_error = http_error(exc)
                else:
                    audit["error"] = {"type": "HTTPError", "status": exc.code}
                    save_audit()
                    raise http_error(exc) from exc
            except (
                http.client.RemoteDisconnected,
                TimeoutError,
                socket.timeout,
                urllib.error.URLError,
                ValueError,
                RuntimeError,
            ) as exc:
                last_error = exc
            if attempt < mode_retries:
                time.sleep(min(5 * (2 ** attempt), 30))

    if last_error is not None:
        audit["error"] = {"type": type(last_error).__name__}
        save_audit()
        raise last_error
    raise RuntimeError("model request failed without an error")
