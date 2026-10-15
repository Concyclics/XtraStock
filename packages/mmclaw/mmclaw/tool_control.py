from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDecision:
    execute: bool
    result: str | None = None
    reason: str | None = None
    signature: str | None = None


class ToolLoopController:
    """Bound one model turn's tool loop and stop repeated failure patterns."""

    def __init__(
        self,
        *,
        max_rounds: int = 6,
        max_calls: int = 12,
        duplicate_limit: int = 1,
        same_error_limit: int = 2,
    ):
        self.max_rounds = max(1, int(max_rounds))
        self.max_calls = max(1, int(max_calls))
        self.duplicate_limit = max(1, int(duplicate_limit))
        self.same_error_limit = max(1, int(same_error_limit))
        self.rounds = 0
        self.requested_calls = 0
        self.executed_calls = 0
        self.blocked_calls = 0
        self.seen_calls: Counter[str] = Counter()
        self.seen_errors: Counter[str] = Counter()
        self.force_final = False
        self.stop_reason: str | None = None
        self.cached_results: dict[str, str] = {}
        self.last_signatures: dict[str, str] = {}
        self.disabled_tools: set[str] = set()
        self.cache_hits = 0

    @classmethod
    def from_config(cls, config: dict[str, Any] | None, *, background: bool = False):
        settings = dict((config or {}).get("tool_control") or {})
        if background:
            return cls(
                max_rounds=settings.get("background_max_rounds", 10),
                max_calls=settings.get("background_max_calls", 30),
                duplicate_limit=settings.get("duplicate_limit", 1),
                same_error_limit=settings.get("same_error_limit", 2),
            )
        return cls(
            max_rounds=settings.get("chat_max_rounds", 6),
            max_calls=settings.get("chat_max_calls", 12),
            duplicate_limit=settings.get("duplicate_limit", 1),
            same_error_limit=settings.get("same_error_limit", 2),
        )

    @staticmethod
    def call_signature(name: str | None, args: Any) -> str:
        normalized = dict(args) if isinstance(args, dict) else args
        # Only normalize proven aliases/defaults, within this request's cache.
        # Date windows, cutoffs, ordering and content filters remain distinct.
        if isinstance(normalized, dict):
            if name == "finance_time_context":
                normalized.setdefault("timezone", "Asia/Shanghai")
            if name in {"finance_market_series_batch", "finance_fundamentals_batch", "finance_graph_analysis", "finance_factor_analysis"}:
                codes = normalized.get("stock_codes", [])
                if isinstance(codes, list):
                    codes = list(dict.fromkeys(codes + ([normalized["stock_code"]] if normalized.get("stock_code") else [])))
                    if codes:
                        normalized["stock_codes"] = codes
                        normalized.pop("stock_code", None)
            if name == "finance_market_series_batch":
                normalized.setdefault("price_basis", "total_return")
        payload = json.dumps(normalized or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        return f"{name or 'unknown'}:{digest}"

    @staticmethod
    def args_hash(args: Any) -> str:
        payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def result_error_key(name: str | None, result: Any) -> str | None:
        text = str(result or "").strip()
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            meta = payload.get("meta")
            status = str(meta.get("status", "")).lower() if isinstance(meta, dict) else ""
            if status in {"error", "failed", "blocked", "invalid_request"}:
                return f"{name or 'unknown'}:status={status}"
        error = bool(
            text.startswith("FinanceDB error:")
            or text.startswith("FinanceDB schema error:")
            or text.startswith("Finance plot error:")
            or text.startswith("Error:")
            or text.startswith("Error executing command:")
            or text.startswith("Traceback (most recent call last)")
            or re.match(r"Return Code [1-9][0-9]*:", text)
        )
        if not error:
            return None
        first = " ".join(text.splitlines()[:2])[:240]
        first = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", first, flags=re.I)
        first = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", first)
        return f"{name or 'unknown'}:{first}"

    @staticmethod
    def row_count(result: Any) -> int | None:
        match = re.search(r'["\']?row_count["\']?\s*[:=]\s*(\d+)', str(result or ""))
        return int(match.group(1)) if match else None

    def begin_round(self) -> bool:
        self.rounds += 1
        if self.rounds <= self.max_rounds:
            return True
        self._stop("max_rounds")
        return False

    def before_call(self, name: str | None, args: Any) -> ToolDecision:
        self.requested_calls += 1
        signature = self.call_signature(name, args)
        if signature in self.cached_results:
            self.cache_hits += 1
            return ToolDecision(False, self.cached_results[signature], "cached_result", signature)
        if name in self.disabled_tools:
            self.blocked_calls += 1
            return ToolDecision(False, self.blocked_result("repeated_error"), "repeated_error", signature)
        if self.force_final or self.executed_calls >= self.max_calls:
            self.blocked_calls += 1
            self._stop(self.stop_reason or "max_calls")
            return ToolDecision(
                False,
                self.blocked_result(self.stop_reason or "max_calls"),
                self.stop_reason or "max_calls",
                signature,
            )
        if self.seen_calls[signature] >= self.duplicate_limit:
            self.blocked_calls += 1
            return ToolDecision(False, self.blocked_result("duplicate_call"), "duplicate_call", signature)
        self.seen_calls[signature] += 1
        self.executed_calls += 1
        self.last_signatures[name] = signature
        return ToolDecision(True, signature=signature)

    def after_call(self, name: str | None, result: Any) -> str | None:
        signature = self.last_signatures.get(name)
        if signature:
            self.cached_results[signature] = str(result or "")
        error_key = self.result_error_key(name, result)
        if not error_key:
            return None
        self.seen_errors[error_key] += 1
        if self.seen_errors[error_key] >= self.same_error_limit:
            self.disabled_tools.add(name)
        return error_key

    def _stop(self, reason: str) -> None:
        self.force_final = True
        if self.stop_reason is None:
            self.stop_reason = reason

    def blocked_result(self, reason: str) -> str:
        labels = {
            "max_rounds": "tool round budget reached",
            "max_calls": "tool call budget reached",
            "duplicate_call": "identical tool call blocked",
            "repeated_error": "same tool error repeated",
        }
        if reason in {"duplicate_call", "repeated_error"}:
            return (
                f"Tool control: {labels[reason]}. Reuse the earlier result or use another relevant tool. "
                "This does not end the research task and does not establish a database-wide data gap."
            )
        return (
            f"Tool control: {labels.get(reason, reason)}. "
            "Do not retry this call. Use evidence already returned, disclose any unresolved limitation, and answer now."
        )

    def final_instruction(self) -> str:
        return (
            "[INTERNAL TOOL CONTROL] No more tool calls are available for this turn "
            f"({self.stop_reason or 'budget'}). Synthesize the best answer from existing results, "
            "state concrete dates and data limitations, and do not request another tool."
        )

    def summary(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "requested_calls": self.requested_calls,
            "executed_calls": self.executed_calls,
            "blocked_calls": self.blocked_calls,
            "cache_hits": self.cache_hits,
            "disabled_tools": sorted(self.disabled_tools),
            "stop_reason": self.stop_reason,
        }
