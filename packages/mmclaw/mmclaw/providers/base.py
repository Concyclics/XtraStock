
from xtrastock.config import bootstrap_environment
bootstrap_environment()

import os


class BaseProvider(object):
    supports_native_tools = False

    def __init__(self, config):
        self.config = config
        self.engine_type = config["engine_type"]
        engine_config = config["engines"][self.engine_type]
        api_key_env = str(engine_config.get("api_key_env") or "").strip()
        self.api_key = os.getenv(api_key_env, "") if api_key_env else engine_config.get("api_key", "")
        self.base_url = engine_config.get("base_url", "").rstrip("/")
        self.model = engine_config["model"]
        self.debug = config.get("debug", False)
        self.stream = config.get("stream", True)
        self.request_timeout = int(engine_config.get("timeout", config.get("provider_timeout", 60)))
        self.reasoning_effort = str(engine_config.get("reasoning_effort") or "").strip()
        self.api_mode = str(engine_config.get("api_mode") or "chat_completions").strip().lower()

    def ask(self, messages, tools=None, retry=1):
        raise NotImplementedError

    def complete(self, messages, tools=None, response_schema=None, timeout=None):
        request_messages = list(messages)
        if response_schema:
            request_messages.append({
                "role": "system",
                "content": "Return JSON matching this schema exactly: " + str(response_schema),
            })
        return self.ask(request_messages, tools=tools)

    def health(self):
        return {
            "provider": self.engine_type,
            "model": self.model,
            "configured": bool(self.api_key or self.engine_type == "codex"),
            "base_url": self.base_url,
        }

    def summarize(self, messages, max_tokens=800):
        prompt = list(messages) + [{
            "role": "user",
            "content": f"Summarize the preceding content faithfully in no more than {int(max_tokens)} tokens.",
        }]
        return self.ask(prompt, tools=None)

    def tool_result_messages(self, tool_calls, results):
        messages = []
        for call, result in zip(tool_calls, results):
            messages.append({
                "role": "user",
                "content": "Tool Output ({}):\n{}".format(call.get("name"), result),
            })
        return messages
