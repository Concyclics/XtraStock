"""Per-turn evidence collection and deterministic finance-answer validation."""

from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import datetime as dt
import hashlib
import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from .prediction_contract import prediction_error
from .reader_language import READER_STYLE_PROMPT, strip_boilerplate


ALLOWED_CLAIM_TYPES = {
    "actual", "forecast", "consensus", "market", "model_signal",
    "document_claim", "graph_association", "calculation", "data_quality",
}
ALLOWED_QUALITY = {
    "verified", "unverified", "conflict", "stale", "incomplete", "rejected",
}
RELATIVE_DATE_RE = re.compile(r"今天|今日|昨天|昨日|本周|最近|近期|当前|现在|最新|截至目前")
ABSOLUTE_DATE_RE = re.compile(r"20\d{2}[-年/]\d{1,2}(?:[-月/]\d{1,2}日?)?")
MONTH_DAY_RE = re.compile(r"(?<!\d)(?:0?[1-9]|1[0-2])月(?:[12]\d|3[01]|0?[1-9])日?(?!\d)")
TIME_OF_DAY_RE = re.compile(r"(?<![\d.])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?![\d.])")
EVIDENCE_ID_RE = re.compile(r"[0-9a-fA-F-]{32,36}:E\d{4}")
SHORT_EVIDENCE_RE = re.compile(r"(?<![A-Za-z0-9:])E\d{4}\b")
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
CNY_AMOUNT_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<number>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?:\*{1,2}|_{1,2})?\s*(?P<scale>亿|万)?元"
)
STOCK_CODE_RE = re.compile(r"(?<!\d)[0368]\d{5}(?!\d)")
FINANCE_FACT_RE = re.compile(
    r"股票|公司|行业|板块|研报|新闻|公告|营收|利润|净利|同比|环比|PE|PB|股价|涨|跌|因子|信号|预测|目标价|评级",
    re.I,
)


def _json_default(value: Any):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _load_env(root: Path) -> dict[str, str]:
    from xtrastock.config import load_legacy_env
    return load_legacy_env()


def persist_model_call(
    turn_id: str, config: dict[str, Any], purpose: str, *,
    prompt_hash: str | None, tool_schema_hash: str | None,
    duration_ms: float, status: str, response: dict[str, Any] | None = None,
    error: Exception | None = None, root: Path | None = None,
) -> None:
    """Best-effort model telemetry without prompt bodies or credentials."""
    try:
        import psycopg
        root = root or Path(__file__).resolve().parents[2]
        env = _load_env(root)
        provider = str((response or {}).get("_mmclaw_provider") or config.get("engine_type") or "unknown")
        engine = (config.get("engines") or {}).get(provider, {}) or {}
        model = str((response or {}).get("_mmclaw_model") or engine.get("model") or engine.get("model_name") or provider)
        usage = (response or {}).get("usage") or {}
        with psycopg.connect(
            host=os.getenv("FINANCE_PGHOST", "127.0.0.1"),
            port=int(os.getenv("FINANCE_PGPORT", "15432")),
            dbname=os.getenv("FINANCE_PGDATABASE", env.get("POSTGRES_DB", "finance_corpus")),
            user=os.getenv("FINANCE_APP_DB_USER", env.get("POSTGRES_APP_USER", "finance_app")),
            password=os.getenv("FINANCE_APP_DB_PASSWORD", env.get("APP_DB_PASSWORD", "")),
            connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=1000",
        ) as conn:
            conn.execute(
                """
                INSERT INTO agent.model_calls (
                  turn_id,provider,model,purpose,prompt_hash,tool_schema_hash,
                  duration_ms,input_tokens,output_tokens,status,error_class
                ) VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    turn_id,provider,model,purpose,prompt_hash,tool_schema_hash,duration_ms,
                    usage.get("prompt_tokens") or usage.get("input_tokens"),
                    usage.get("completion_tokens") or usage.get("output_tokens"),
                    status,type(error).__name__ if error else None,
                ),
            )
    except Exception:
        return


def _walk_numbers(value: Any):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        yield float(value)
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_numbers(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_numbers(nested)
    elif isinstance(value, str):
        try:
            yield float(value)
        except (TypeError, ValueError):
            return


def _walk_stock_codes(value: Any):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_stock_codes(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_stock_codes(nested)
    elif value is not None:
        text = str(value)
        yield from STOCK_CODE_RE.findall(text)


def _answer_numbers(text: str) -> list[tuple[float, str]]:
    scrubbed = EVIDENCE_ID_RE.sub("", text)
    # Document/entity UUIDs are identifiers, not numeric financial assertions.
    scrubbed = re.sub(r'\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b', '', scrubbed)
    # Same-month shorthand must start with an explicit date. Do not consume
    # an adjacent percentage/amount or an unanchored numerical range.
    day = r"(?:[12]\d|3[01]|0?[1-9])(?!\d)"
    scrubbed = re.sub(rf"(?<!\d)(?:0?[1-9]|1[0-2])月{day}日(?:\s*(?:或|、|至|到|—|~)\s*{day}日)+", "", scrubbed)
    scrubbed = re.sub(rf"20\d{{2}}-\d{{1,2}}-{day}(?:\s*[、，,]\s*{day}(?![\d.%％元亿万]))+", "", scrubbed)
    scrubbed = re.sub(r"20\d{2}-\d{1,2}-\d{1,2}(?:\s*(?:或|、|至|到|—|~)\s*(?:0?[1-9]|1[0-2])-\d{1,2}(?!\d))+", "", scrubbed)
    scrubbed = ABSOLUTE_DATE_RE.sub("", scrubbed)
    scrubbed = MONTH_DAY_RE.sub("", scrubbed)
    # Like dates and stock codes, clock components are not financial metrics.
    # In particular, available_at timestamps must not turn into claims of 16/0.
    scrubbed = TIME_OF_DAY_RE.sub("", scrubbed)
    result = []
    for match in NUMBER_RE.finditer(scrubbed):
        token = match.group(0)
        plain = token.rstrip("%").replace(",", "")
        try:
            value = float(plain)
        except ValueError:
            continue
        # Keep leading-zero symbols (e.g. 002371) in the stock-code validator,
        # rather than also interpreting them as an unsupported numeric metric.
        if STOCK_CODE_RE.fullmatch(plain) and not token.endswith("%"):
            continue
        if 1900 <= value <= 2100 and "." not in plain:
            continue
        line_start = scrubbed.rfind("\n", 0, match.start()) + 1
        if re.fullmatch(r"[\s#>*_-]*", scrubbed[line_start:match.start()]) and scrubbed[match.end():match.end()+1] in {".", "、", ")", "）"}:
            continue
        result.append((value, token))
    return result


def _number_supported(value: float, evidence_values: set[float], user_values: set[float]) -> bool:
    candidates = evidence_values | user_values
    for candidate in candidates:
        variants = {candidate}
        if abs(candidate) <= 10:
            variants.add(candidate * 100)
        if abs(candidate) >= 1:
            variants.add(candidate / 100)
        if any(abs(value - variant) <= max(0.001, abs(variant) * 0.0005) for variant in variants):
            return True
    return False


def _rounded_number_supported(value, token, candidates):
    plain = token.rstrip("%").replace(",", "")
    if "." not in plain:
        return False
    places = len(plain.split(".", 1)[1])
    tolerance = 0.5 * 10 ** -places + 1e-10
    for candidate in candidates:
        variants = [candidate]
        if token.endswith("%") and abs(candidate) <= 10:
            variants.append(candidate * 100)
        if any(abs(value - number) < tolerance for number in variants):
            return True
    return False


def _directional_number(value, token, paragraph):
    """A positive magnitude in '下跌 13.7%' represents a negative return."""
    if value < 0:
        return value
    # Match the token at a numeric boundary; '1.3' must not bind to '11.3'.
    for match in re.finditer(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", paragraph):
        prefix = paragraph[max(0, match.start()-100):match.start()]
        listed = r"(?:[-+]?\d+(?:\.\d+)?\s*%?\s*(?:和|及|、|与)\s*)*"
        if re.search(r"(?:下跌|下降|减少|回落|回撤|跌幅|下滑|跌)\s*(?:了|为|约|达|达到|扩大至)?\s*[*_]*\s*" + listed + "$", prefix):
            return -value
    return value


def _document_text(item):
    value = item.get("value") or {}
    if not isinstance(value, dict):
        return ""
    # A report excerpt retained by the temporal graph is still quotable as a
    # report claim, not as a verified financial fact or a graph prediction.
    report_excerpt = (item.get("claim_type") == "graph_association"
                      and value.get("claim_class") == "extracted_claim"
                      and value.get("source_system") == "research.reports"
                      and (value.get("source_document_id") or item.get("source_document_id")))
    if item.get("claim_type") != "document_claim" and not report_excerpt:
        return ""
    return "\n".join(str(value.get(key) or "") for key in (
        "title", "snippet", "text", "evidence_text", "body", "body_excerpt", "content_text", "summary"))


def _reported_value_supported(token, paragraph, items, *, money=False):
    """Verify a reported value in the cited source text, never as an actual fact.

    Retrieval snippets are prose, so walking JSON numbers alone misses them.
    Retain attribution and unit/sign matching instead of treating every number
    in every retrieved document as evidence for any claim.
    """
    if not re.search(r"研报|报告|报道|资料(?:记载|称|提及)|文中|原文|机构(?:观点|称|认为)|券商", paragraph):
        return False
    normalize = lambda s: re.sub(r"[\s,*_]", "", s)
    if money:
        candidate = normalize(token)
        for item in items:
            source = normalize(_document_text(item))
            if not source:
                continue
            # Match the entire unit, including /吨 or /米 when present.
            for match in re.finditer(re.escape(candidate), source):
                before = source[max(0, match.start()-1):match.start()]
                after = source[match.end():match.end()+1]
                if before and (before.isdigit() or before == "."):
                    continue
                if before == "-" and not source[max(0,match.start()-2):match.start()-1].isdigit():
                    continue
                if after == "/" and "/" not in candidate:
                    continue
                return True
        return False
    requested = normalize(token)
    for item in items:
        source = _document_text(item)
        for match in NUMBER_RE.finditer(source):
            observed = match.group(0)
            if observed.startswith("-") and match.start() and source[match.start()-1].isdigit():
                observed = observed[1:]  # upper endpoint of e.g. 13-13.3
            if observed.endswith("%") == token.endswith("%"):
                try:
                    expected_value = _directional_number(float(requested.rstrip("%")), token, paragraph)
                    source_value = _directional_number(float(normalize(observed).rstrip("%")), observed,
                                                       source[max(0,match.start()-100):match.end()])
                    if abs(expected_value-source_value) < 1e-9:
                        return True
                except ValueError:
                    pass
    return False


def _cited_paragraph(text, offset, items):
    start = text.rfind("\n\n", 0, offset) + 2
    if start == 1:
        start = 0
    end = text.find("\n\n", offset)
    paragraph = text[start:end if end >= 0 else len(text)]
    return paragraph, [items[key] for key in EVIDENCE_ID_RE.findall(paragraph) if key in items]


def _without_cited_factor_labels(text: str, items: dict[str, dict[str, Any]]) -> str:
    """A cited factor's exact name is metadata, not a new numeric assertion.

    Do not add horizon digits to the allowed values: that would also authorize
    unrelated returns or scores with the same number. Match only complete names
    from evidence cited in the same paragraph.
    """
    paragraphs = re.split(r"(\n\s*\n)", text)
    for index, paragraph in enumerate(paragraphs):
        labels = set()
        horizons = set()
        for key in EVIDENCE_ID_RE.findall(paragraph):
            item = items.get(key, {})
            value = item.get("value") or {}
            if isinstance(value, dict) and (item.get("claim_type") == "market"
                                           or item.get("metric_id") == "finance_market_screen"):
                for days in (5, 20):
                    if value.get(f"ret_{days}d") is not None:
                        paragraph = re.sub(rf"(?<!\d){days}(?:个交易|交易)?日", "所述期间", paragraph)
            if item.get("claim_type") != "model_signal":
                continue
            if isinstance(value, dict):
                labels.update(str(value.get(field) or "") for field in ("factor_name", "display_name"))
            labels.add(str(item.get("metric_id") or ""))
        for label in labels:
            horizons.update(re.findall(r"(?<!\d)(\d+)d\b|(?<!\d)(\d+)(?:个交易|交易)?日", label))
        for parts in horizons:
            days = next(x for x in parts if x)
            paragraph = re.sub(rf"(?<!\d){days}(?:个交易|交易)?日", "所述窗口", paragraph)
        for label in sorted(labels, key=len, reverse=True):
            if label and re.search(r"\d", label):
                paragraph = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])", "因子名称", paragraph)
        paragraphs[index] = paragraph
    return "".join(paragraphs)


def with_quality_evidence(tool_name: str, result: str) -> str:
    """Make returned coverage/count metadata citable, without changing its meaning.

    These are query coverage measurements, never returns or company financials.
    Only explicit numeric fields are admitted; errors and arbitrary metadata are
    excluded. The same result is delivered to the model and the evidence ledger.
    """
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return result
    meta = payload["meta"]
    if meta.get("status") not in {"ok", "partial", "empty", "degraded"}:
        return result
    fields = ("coverage_pct", "row_count", "expected_factor_stock_pairs", "returned_factor_stock_pairs",
              "production_count", "evidence_count_available", "evidence_count_returned",
              "rows_omitted_from_output", "evidence_omitted_from_output")
    values = {k:meta[k] for k in fields if isinstance(meta.get(k), (int, float))
              and not isinstance(meta[k], bool) and math.isfinite(meta[k])}
    if not values:
        return result
    items = payload.get("evidence")
    if not isinstance(items, list):
        return result
    prefix = str(meta.get("request_id") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", prefix):
        prefix = hashlib.sha256((tool_name+result).encode()).hexdigest()[:32]
    key = prefix+":E0000"
    if any(item.get("evidence_id") == key for item in items if isinstance(item, dict)):
        return result
    items.append({"evidence_id":key, "claim_type":"data_quality", "metric_id":"query_coverage",
                  "value":values, "unit":"query counts and coverage percentage; not financial performance",
                  "source_id":tool_name, "as_of_date":meta.get("data_through"),
                  "available_at":meta.get("decision_cutoff"), "quality_status":"unverified"})
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _quality_number_supported(value, token, paragraph, items):
    values = [item.get("value") for item in items if item.get("metric_id") == "query_coverage"
              and item.get("claim_type") == "data_quality" and isinstance(item.get("value"), dict)]
    if not values:
        return False
    if token.endswith("%"):
        if not re.search(rf"(?:覆盖率?|coverage)[^。；\n\d]{{0,12}}{re.escape(token)}", paragraph, re.I):
            return False
        candidates = {v["coverage_pct"] for v in values if "coverage_pct" in v}
    else:
        if not re.search(rf"(?<![\d.]){re.escape(token)}\s*(?:个|种|项|条|组|对|笔|records|signals)", paragraph, re.I):
            return False
        candidates = {n for v in values for k,n in v.items() if k != "coverage_pct"}
    return any(abs(value-n) <= 1e-9 for n in candidates) or _rounded_number_supported(value, token, candidates)


def _cny_amounts(item: dict[str, Any]):
    """Only typed amounts qualify for 元/万元/亿元 formatting conversions."""
    row = item.get("value")
    unit = (row.get("unit") if isinstance(row, dict) else None) or item.get("unit")
    scales = {"CNY": Decimal(1), "元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000)}
    values = []
    if unit in scales:
        values = [row.get("value")] if isinstance(row, dict) else [row]
        if isinstance(row, dict) and item.get("claim_type") == "market":
            values += [row.get(key) for key in ("open", "high", "low", "close", "amount")]
        if isinstance(row, dict) and item.get("claim_type") == "calculation":
            values += [row.get("result"), row.get("rounded_result")]
    elif item.get("claim_type") == "market" and isinstance(row, dict) and unit == "CNY; volume/amount source units retained":
        # Batch OHLC prices are explicitly yuan; volume and turnover retain
        # source-specific units and must not be admitted as yuan amounts.
        values = [row.get(key) for key in ("open", "high", "low", "close")]
        unit = "CNY"
    elif item.get("claim_type") == "market" and isinstance(row, dict) and unit in (None, "price"):
        # Local A-share market rows carry explicit price/turnover fields.
        if re.fullmatch(r"[0368]\d{5}", str(row.get("stock_code") or item.get("entity_id") or "")):
            values = [row.get(key) for key in ("open", "high", "low", "close", "amount")]
            unit = "CNY"
    elif item.get("metric_id") == "finance_market_screen" and isinstance(row, dict):
        # A snapshot has mixed metrics. Only its close is an explicitly yuan
        # price; PE/PB, ranks and decimal returns are not currency amounts.
        values = [row.get("close")]
        unit = "CNY"
    elif (item.get("claim_type") == "calculation" and isinstance(row, dict)
          and str(item.get("metric_id") or "").startswith("period_return_pct_")
          and re.fullmatch(r"[0368]\d{5}", str(row.get("stock_code") or ""))):
        # The deterministic A-share return calculator retains its two input
        # close prices in yuan even though its output return is in percent.
        values = [row.get("start_close"), row.get("end_close")]
        unit = "CNY"
    for value in values:
        try:
            amount = Decimal(str(value)) * scales[unit]
            if amount.is_finite():
                yield amount
        except (InvalidOperation, KeyError, TypeError):
            continue


class EvidenceStore:
    def __init__(self, turn_id: str, user_text: str, root: Path | None = None):
        self.turn_id = turn_id
        self.user_text = str(user_text or "")
        self.root = root or Path(__file__).resolve().parents[2]
        self.items: dict[str, dict[str, Any]] = {}
        self.tool_errors: list[dict[str, str]] = []
        self.tool_calls: set[str] = set()

    def add_tool_result(self, tool_name: str, result: Any) -> int:
        self.tool_calls.add(tool_name)
        text = str(result or "")
        try:
            payload = json.loads(text)
        except Exception:
            if text.startswith("FinanceDB error:") or "Traceback" in text:
                self.tool_errors.append({"tool": tool_name, "error": text[:500]})
            return 0
        # Generic capabilities may return JSON lists/scalars rather than a
        # finance envelope. Their execution receipt remains usable by the loop.
        if not isinstance(payload, dict):
            return 0
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if str(meta.get("status") or "").lower() in {"error", "failed", "blocked", "invalid_request"}:
            self.tool_errors.append({"tool": tool_name, "error": json.dumps(meta, ensure_ascii=False)[:500]})
            return 0
        added = 0
        for raw in payload.get("evidence") or []:
            if not isinstance(raw, dict) or not raw.get("evidence_id"):
                continue
            item = dict(raw)
            item["tool_name"] = tool_name
            item["claim_type"] = item.get("claim_type") if item.get("claim_type") in ALLOWED_CLAIM_TYPES else "document_claim"
            item["quality_status"] = item.get("quality_status") if item.get("quality_status") in ALLOWED_QUALITY else "unverified"
            self.items[str(item["evidence_id"])] = item
            added += 1
        return added

    def compact_ledger(self, limit: int = 80) -> list[dict[str, Any]]:
        keys = (
            "evidence_id", "claim_type", "entity_id", "metric_id", "value", "unit",
            "period_end", "as_of_date", "available_at", "source_id",
            "source_document_id", "quality_status", "tool_name",
        )
        return [{key: item.get(key) for key in keys} for item in list(self.items.values())[:limit]]

    def normalize_citations(self, text):
        def expand(match):
            group = match.group(0)
            full = EVIDENCE_ID_RE.findall(group)
            prefixes = {key.rsplit(":",1)[0] for key in full if key in self.items}
            if len(prefixes) != 1:
                return group
            prefix = next(iter(prefixes))
            return SHORT_EVIDENCE_RE.sub(
                lambda short: prefix+":"+short.group(0) if prefix+":"+short.group(0) in self.items else short.group(0),
                group)
        # Resolve only an explicit grouped citation with one known prefix. Never
        # guess a source for a free-floating shorthand or a mixed-prefix group.
        text = re.sub(r"\[[^\]\n]*\]", expand, str(text or ""))
        # The JSON model also writes unbracketed chains, e.g. UUID:E0001、E0002.
        # A chain is locally unambiguous; a standalone E0002 remains invalid.
        chain = EVIDENCE_ID_RE.pattern + r"(?:\s*[、,，]\s*E\d{4}\b)+"
        return re.sub(chain, expand, text)

    def persist(self) -> None:
        if not self.items:
            return
        try:
            import psycopg
            from psycopg.types.json import Jsonb
            env = _load_env(self.root)
            with psycopg.connect(
                host=os.getenv("FINANCE_PGHOST", "127.0.0.1"),
                port=int(os.getenv("FINANCE_PGPORT", "15432")),
                dbname=os.getenv("FINANCE_PGDATABASE", env.get("POSTGRES_DB", "finance_corpus")),
                user=os.getenv("FINANCE_APP_DB_USER", env.get("POSTGRES_APP_USER", "finance_app")),
                password=os.getenv("FINANCE_APP_DB_PASSWORD", env.get("APP_DB_PASSWORD", "")),
                connect_timeout=3,
                options="-c statement_timeout=3000 -c lock_timeout=1000",
            ) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO agent.turn_evidence (
                          turn_id,evidence_id,tool_name,claim_type,entity_id,metric_id,
                          value_json,unit,period_end,as_of_date,available_at,source_id,
                          source_document_id,quality_status,evidence_json
                        ) VALUES (
                          %s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                        ) ON CONFLICT (turn_id,evidence_id) DO UPDATE SET
                          tool_name=EXCLUDED.tool_name,claim_type=EXCLUDED.claim_type,
                          entity_id=EXCLUDED.entity_id,metric_id=EXCLUDED.metric_id,
                          value_json=EXCLUDED.value_json,unit=EXCLUDED.unit,
                          period_end=EXCLUDED.period_end,as_of_date=EXCLUDED.as_of_date,
                          available_at=EXCLUDED.available_at,source_id=EXCLUDED.source_id,
                          source_document_id=EXCLUDED.source_document_id,
                          quality_status=EXCLUDED.quality_status,evidence_json=EXCLUDED.evidence_json
                        """,
                        [(
                            self.turn_id, item["evidence_id"], item["tool_name"], item["claim_type"],
                            item.get("entity_id"), item.get("metric_id"), Jsonb(item.get("value")),
                            item.get("unit"), item.get("period_end"), item.get("as_of_date"),
                            item.get("available_at"), item.get("source_id"), item.get("source_document_id"),
                            item["quality_status"], Jsonb(item),
                        ) for item in self.items.values()],
                    )
        except Exception as exc:
            self.tool_errors.append({"tool": "evidence_persist", "error": f"{type(exc).__name__}: {exc}"[:500]})

    def validate(self, answer: str, *, require_date=True) -> dict[str, Any]:
        text = str(answer or "")
        violations = []
        if str(answer or "").strip() and not strip_boilerplate(answer).strip():
            violations.append({"code": "boilerplate_only_answer", "message": "Answer the request with supported content; disclaimers alone are not an answer."})
        cited = set(EVIDENCE_ID_RE.findall(text))
        unknown = sorted(cited - set(self.items))
        if unknown:
            violations.append({"code": "unknown_evidence_id", "values": unknown})
        short_refs = SHORT_EVIDENCE_RE.findall(text)
        if short_refs:
            violations.append({"code":"incomplete_evidence_id", "values":sorted(set(short_refs))})
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph_items = [self.items[key] for key in EVIDENCE_ID_RE.findall(paragraph) if key in self.items]
            prediction_violation=prediction_error(paragraph,paragraph_items)
            if prediction_violation:
                violations.append({'code':prediction_violation})
            if ("中证网" in paragraph and paragraph_items
                    and all(item.get("source_id") == "cninfo_official" for item in paragraph_items)):
                violations.append({"code": "source_attribution_mismatch", "expected": "巨潮资讯（公司公告）",
                                   "actual": "中证网"})
                break
        factual = bool(FINANCE_FACT_RE.search(text))
        explicit_gap = bool(re.search(r"无法|没有足够|证据不足|数据不足|缺失|未查询|不可用|未入库", text))
        numeric_text = _without_cited_factor_labels(text, self.items)
        numbers = _answer_numbers(numeric_text)
        if (factual or numbers) and self.items and not cited:
            violations.append({"code": "missing_evidence_citation"})
        if factual and not self.items and not explicit_gap:
            violations.append({"code": "factual_claim_without_evidence"})
        if require_date and RELATIVE_DATE_RE.search(self.user_text) and (factual or numbers) and not ABSOLUTE_DATE_RE.search(text):
            violations.append({"code": "relative_date_without_absolute_date"})
        evidence_values = set()
        for item in self.items.values():
            if item.get("metric_id") == "query_coverage":
                continue
            evidence_values.update(_walk_numbers(item.get("value")))
        user_values = {value for value, _ in _answer_numbers(self.user_text)}
        # Currency display units are formatting, not invented new metrics. Match
        # in CNY against typed, cited amounts with only display-rounding tolerance;
        # do not apply generic percent scaling to currency or unrelated metadata.
        money_matches = list(CNY_AMOUNT_RE.finditer(text))
        money_values = {value for key in cited if key in self.items
                        for value in _cny_amounts(self.items[key])}
        unsupported = []
        for match in money_matches:
            paragraph, paragraph_items = _cited_paragraph(text, match.start(), self.items)
            suffix = re.match(r"/(?:[\u4e00-\u9fffA-Za-z]+)", text[match.end():])
            reported_token = match.group(0) + (suffix.group(0) if suffix else "")
            if _reported_value_supported(reported_token, paragraph, paragraph_items, money=True):
                continue
            number = match.group("number").replace(",", "")
            scale = {None: Decimal(1), "万": Decimal(10000), "亿": Decimal(100000000)}[match.group("scale")]
            places = len(number.split(".")[1]) if "." in number else 0
            amount = Decimal(number) * scale
            tolerance = Decimal("0.5") * (Decimal(10) ** -places) * scale
            local_money = set(money_values)
            if scale == 1 and re.search(r"每股|EPS", paragraph, re.I):
                for item in paragraph_items:
                    row = item.get("value") or {}
                    if (isinstance(row, dict) and (row.get("unit") or item.get("unit")) in {"CNY/share", "元/股"}
                            and str(item.get("metric_id") or row.get("metric_code") or "").startswith("eps")):
                        try: local_money.add(Decimal(str(row.get("value"))))
                        except InvalidOperation: pass
            if not any(abs(amount - value) <= tolerance for value in local_money):
                unsupported.append(match.group(0))
        for paragraph in re.split(r"\n\s*\n", numeric_text):
            paragraph_items = [self.items[key] for key in EVIDENCE_ID_RE.findall(paragraph) if key in self.items]
            for value, token in _answer_numbers(CNY_AMOUNT_RE.sub("", paragraph)):
                directional = _directional_number(value, token, paragraph)
                if _number_supported(directional, evidence_values, user_values):
                    continue
                local_values = {number for item in paragraph_items if item.get("metric_id") != "query_coverage"
                                for number in _walk_numbers(item.get("value"))}
                if _rounded_number_supported(directional, token, local_values):
                    continue
                if _quality_number_supported(directional, token, paragraph, paragraph_items):
                    continue
                if not _reported_value_supported(token, paragraph, paragraph_items):
                    unsupported.append(token)
        if unsupported:
            violations.append({"code": "unsupported_numeric_value", "values": unsupported[:20]})
        if numbers and not self.items:
            violations.append({"code": "numeric_claim_without_evidence"})
        scrubbed = EVIDENCE_ID_RE.sub("", text)
        answer_codes = set(STOCK_CODE_RE.findall(scrubbed))
        supported_codes = set(STOCK_CODE_RE.findall(self.user_text))
        for item in self.items.values():
            supported_codes.update(_walk_stock_codes(item.get("entity_id")))
            supported_codes.update(_walk_stock_codes(item.get("value")))
        unsupported_codes = sorted(answer_codes - supported_codes)
        if unsupported_codes:
            violations.append({"code": "unsupported_stock_code", "values": unsupported_codes})
        return {
            "passed": not violations,
            "violations": violations,
            "cited_evidence_ids": sorted(cited),
            "evidence_count": len(self.items),
            "tool_errors": self.tool_errors,
        }

    def repair_prompt(self, draft: str, validation: dict[str, Any]) -> str:
        cited = set(EVIDENCE_ID_RE.findall(draft))
        candidates = self.compact_ledger(len(self.items))
        # A long, ascending price series must not crowd out later tools' facts
        # or the newest observation. Preserve cited claims first, then distribute
        # the remaining budget across tools and metrics, starting with their
        # newest evidence. A bundle's news must not crowd out its financial facts.
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in sorted(candidates, key=lambda item: str(
            item.get("as_of_date") or item.get("period_end") or item.get("available_at") or ""
        ), reverse=True):
            key = tuple(str(item.get(field) or "") for field in ("tool_name", "claim_type", "metric_id"))
            groups.setdefault(key, []).append(item)
        ordered = [item for item in candidates if item.get("evidence_id") in cited]
        while any(groups.values()):
            for items in groups.values():
                if items:
                    ordered.append(items.pop(0))
        entries = []
        selected = set()
        for item in ordered:
            if item["evidence_id"] in selected:
                continue
            proposed = json.dumps(entries + [item], ensure_ascii=False, default=_json_default)
            if len(proposed) <= 18000:
                entries.append(item)
                selected.add(item["evidence_id"])
        ledger = json.dumps(entries, ensure_ascii=False, default=_json_default)
        return (
            "Repair ONLY the unsupported claims in the current draft using the evidence ledger. "
            "Preserve its task, company focus, section order and all supported findings. "
            "Do not replace a stock-trend answer with a previous sector overview or a generic warning. "
            "Return only the corrected current-stage user-facing answer. "
            "Do not add unsupported numbers, dates, entities, or causal claims. Cite exact evidence_id values "
            "next to each factual or numeric claim. Preserve actual/forecast/consensus/model_signal distinctions. "
            "Graph associations are not causal proof. If one field is missing, omit or qualify only that field. "
            "A negative return can be written as a positive magnitude with 下跌/下降; keep direction correct. "
            "Numbers printed in a cited report snippet are usable when explicitly attributed to that report, "
            "with original units and observation dates; do not relabel them as independently verified actuals. "
            "Keep supported financial amounts and their reporting periods; do not replace them with a list of field names. "
            "CNY amounts may be formatted as 元/万元/亿元 with normal rounding and the explicit unit. "
            "For such conversion, cite the matching typed CNY financial fact, entity, metric and reporting period "
            "from this ledger. Do not repeatedly cite an untyped document excerpt that lacks its currency header; "
            "the same digits alone do not establish CNY units. "
            "cninfo_official means 巨潮资讯（公司公告）, not 中证网. "
            "The ledger below is the complete set of allowed citations for this repair. "
            f"It contains {len(entries)} of {len(candidates)} accepted evidence entries. "
            "Omission from this budgeted ledger does not prove a database data gap. "
            "Copy evidence_id strings exactly, including every UUID character; never reconstruct IDs "
            "from the draft or violation list. Omit claims that cannot be supported by this ledger.\n\n"
            + READER_STYLE_PROMPT + "\n"
            + f"Validation violations:\n{json.dumps(validation['violations'], ensure_ascii=False)}\n\n"
            f"Draft:\n{draft}\n\nEvidence ledger:\n{ledger}"
        )

    def deterministic_fallback(self, validation: dict[str, Any]) -> str:
        return self.supported_answer("")

    def supported_answer(self, draft: str, *, fallback_to_facts=True) -> str:
        """Keep independently supported paragraphs when repair is incomplete.

        A bad paragraph must not erase the other researched findings. If no
        draft survives, report concrete sourced facts rather than an audit error.
        """
        accepted = []
        for paragraph in re.split(r"\n\s*\n", str(draft or "")):
            if (EVIDENCE_ID_RE.search(paragraph)
                    and self.validate(paragraph, require_date=False)["passed"]):
                accepted.append(paragraph.strip())
                continue
            # Lists/tables often share one blank-line paragraph. Keep the good
            # cited rows instead of discarding every row for one bad number.
            for line in paragraph.splitlines():
                if (re.match(r"\s*(?:[-*]|\d+[.)、）]|\|)", line)
                        and EVIDENCE_ID_RE.search(line)
                        and self.validate(line, require_date=False)["passed"]):
                    if line.lstrip().startswith("|"):
                        line = "- " + "；".join(x.strip() for x in line.strip().strip("|").split("|"))
                    accepted.append(line.strip())
        if accepted:
            date = ABSOLUTE_DATE_RE.search(str(draft))
            prefix = f"资料日期：{date.group(0)}。\n\n" if date and not ABSOLUTE_DATE_RE.search(accepted[0]) else ""
            return prefix + "\n\n".join(accepted)
        if not fallback_to_facts:
            return ""
        facts = []
        for key, item in self.items.items():
            if item.get("claim_type") == "data_quality":
                continue
            row = item.get("value") or {}
            if not isinstance(row, dict):
                continue
            date = str(item.get("as_of_date") or item.get("period_end") or item.get("available_at") or "")[:10]
            name = str(row.get("stock_name") or row.get("stock_code") or "")
            if item.get("claim_type") == "market" and row.get("close") is not None:
                text = f"{date}，{name}收盘价 {row['close']} 元。[{key}]"
            elif item.get("claim_type") == "actual" and row.get("value") is not None:
                labels = {"revenue":"营业收入", "net_profit_parent":"归母净利润",
                          "operating_cash_flow":"经营现金流", "total_assets":"总资产",
                          "total_liabilities":"总负债", "eps_basic":"每股收益"}
                metric = str(row.get("metric_name") or labels.get(item.get("metric_id")) or "已披露数值")
                unit = row.get("unit") or item.get("unit") or ""
                unit = "元" if unit == "CNY" else unit
                text = f"{date}，{name}{metric}为 {row['value']} {unit}。[{key}]"
            elif _document_text(item) and row.get("snippet"):
                snippet = str(row['snippet'])[:650]
                text = f"{date}，研报《{row.get('title') or '相关资料'}》记载：\n{snippet} [{key}]"
            else:
                continue
            if self.validate(text, require_date=False)["passed"]:
                facts.append(text)
            if len(facts) >= 4:
                break
        if facts:
            return "\n\n".join(facts)
        failed = {x.get("tool") for x in self.tool_errors}
        if failed:
            labels = {"finance_document_retrieve":"相关研报原文", "finance_market_series_batch":"所查公司的行情",
                      "finance_market_screen":"板块估值与走势", "finance_research_bundle":"公司研究资料", "finance_research_intelligence":"研究关注与模型复盘",
                      "finance_fundamentals_batch":"公司已披露财务"}
            subjects = list(dict.fromkeys(labels[x] for x in failed if x in labels))
            if subjects:
                return "本次未能取回" + "、".join(subjects) + "，这些部分尚未完成查询。"
        return "本次查询没有返回可引用的相关资料。"

    def persist_audit(self, draft: str, final: str, status: str, validation: dict[str, Any], repair_attempted: bool) -> None:
        try:
            import psycopg
            from psycopg.types.json import Jsonb
            env = _load_env(self.root)
            with psycopg.connect(
                host=os.getenv("FINANCE_PGHOST", "127.0.0.1"),
                port=int(os.getenv("FINANCE_PGPORT", "15432")),
                dbname=os.getenv("FINANCE_PGDATABASE", env.get("POSTGRES_DB", "finance_corpus")),
                user=os.getenv("FINANCE_APP_DB_USER", env.get("POSTGRES_APP_USER", "finance_app")),
                password=os.getenv("FINANCE_APP_DB_PASSWORD", env.get("APP_DB_PASSWORD", "")),
                connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=1000",
            ) as conn:
                conn.execute(
                    """
                    INSERT INTO agent.answer_audits (
                      turn_id,draft_hash,final_hash,validation_status,repair_attempted,
                      violations,evidence_count
                    ) VALUES (%s::uuid,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.turn_id, hashlib.sha256(draft.encode()).hexdigest(),
                        hashlib.sha256(final.encode()).hexdigest(), status, repair_attempted,
                        Jsonb(validation.get("violations") or []), len(self.items),
                    ),
                )
        except Exception as exc:
            self.tool_errors.append({"tool": "audit_persist", "error": f"{type(exc).__name__}: {exc}"[:500]})
