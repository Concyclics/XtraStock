from .legacy import compress_image, prepare_image_content
from .codex import CodexProvider
from .legacy import Engine as LegacyEngine
from .openai_compatible import OpenAICompatibleProvider
from .vertex_ai import VertexAIProvider


OPENAI_COMPATIBLE_NATIVE_PROVIDERS = {
    "openai",
    "google",
    "deepseek",
    "openrouter",
    "kimi",
    "kimi_ai",
    "kimi_cn",
    "minimax_io",
    "minimax_cn",
}


class LegacyProviderAdapter(object):
    supports_native_tools = False

    def __init__(self, config):
        self._engine = LegacyEngine(config)

    def ask(self, messages, tools=None, retry=1):
        return self._engine.ask(messages, tools=tools, retry=retry)

    def tool_result_messages(self, tool_calls, results):
        messages = []
        for call, result in zip(tool_calls, results):
            messages.append({
                "role": "user",
                "content": "Tool Output ({}):\n{}".format(call.get("name"), result),
            })
        return messages

    def complete(self, messages, tools=None, response_schema=None, timeout=None):
        del response_schema, timeout
        return self.ask(messages, tools=tools)

    def health(self):
        return {"provider": "legacy", "configured": True}

    def summarize(self, messages, max_tokens=800):
        return self.ask(list(messages) + [{"role": "user", "content": f"Summarize in at most {max_tokens} tokens."}])


class Engine(object):
    def __init__(self, config):
        self.config = config
        self.engine_type = config["engine_type"]
        self.provider = self._make_provider(config)
        self.fallback_providers = []
        for engine_type in config.get("provider_fallbacks", []) or []:
            if engine_type == self.engine_type or engine_type not in config.get("engines", {}):
                continue
            fallback_config = dict(config)
            fallback_config["engine_type"] = engine_type
            self.fallback_providers.append(self._make_provider(fallback_config))

    def _make_provider(self, config):
        engine_type = config.get("engine_type")
        if engine_type == "codex":
            return CodexProvider(config)
        if engine_type == "vertex_ai":
            return VertexAIProvider(config)
        if engine_type in OPENAI_COMPATIBLE_NATIVE_PROVIDERS or engine_type.startswith("openai_compatible_"):
            return OpenAICompatibleProvider(config)
        return LegacyProviderAdapter(config)

    @property
    def supports_native_tools(self):
        return bool(getattr(self.provider, "supports_native_tools", False))

    def ask(self, messages, tools=None, retry=1):
        last_error = None
        for provider in [self.provider, *self.fallback_providers]:
            try:
                response = provider.ask(messages, tools=tools, retry=retry)
                if isinstance(response, dict):
                    response.setdefault("_mmclaw_provider", getattr(provider, "engine_type", provider.__class__.__name__))
                    response.setdefault("_mmclaw_model", getattr(provider, "model", "unknown"))
                return response
            except Exception as exc:
                last_error = exc
        raise last_error

    def complete(self, messages, tools=None, response_schema=None, timeout=None):
        last_error = None
        for provider in [self.provider, *self.fallback_providers]:
            try:
                response = provider.complete(messages, tools=tools, response_schema=response_schema, timeout=timeout)
                if isinstance(response, dict):
                    response.setdefault("_mmclaw_provider", getattr(provider, "engine_type", provider.__class__.__name__))
                    response.setdefault("_mmclaw_model", getattr(provider, "model", "unknown"))
                return response
            except Exception as exc:
                last_error = exc
        raise last_error

    def health(self):
        return [provider.health() for provider in [self.provider, *self.fallback_providers]]

    def summarize(self, messages, max_tokens=800):
        last_error = None
        for provider in [self.provider, *self.fallback_providers]:
            try:
                return provider.summarize(messages, max_tokens=max_tokens)
            except Exception as exc:
                last_error = exc
        raise last_error

    def tool_result_messages(self, tool_calls, results):
        return self.provider.tool_result_messages(tool_calls, results)


__all__ = ["Engine", "compress_image", "prepare_image_content"]
