# -*- coding: utf-8 -*-
"""Postgres storage adapter for Eastmoney crawlers.

This class keeps the existing fetcher contract (`upsert`, `write_parquet`,
`set_cursor`, `list_instruments`) while writing directly to finance_corpus.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb as PsyJsonb

from eastmoney.secid import split_secid

TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "eastmoney"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def _connect_kwargs() -> dict[str, Any]:
    from xtrastock.config import Settings
    return Settings().postgres_kwargs('app')


def _clean(v: Any) -> Any:
    if v is None:
        return None
    try:
        missing = pd.isna(v)
        if isinstance(missing, bool) and missing:
            return None
    except Exception:
        pass
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float, str, bool, dt.date, dt.datetime)):
        return v
    return str(v)


def _json_ready(v: Any) -> Any:
    if v is None:
        return None
    try:
        missing = pd.isna(v)
        if isinstance(missing, bool) and missing:
            return None
    except Exception:
        pass
    if isinstance(v, dict):
        return {str(k): _json_ready(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_json_ready(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            return _json_ready(v.item())
        except Exception:
            pass
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def Jsonb(v: Any) -> PsyJsonb:
    return PsyJsonb(_json_ready(v))


def _num(v: Any) -> Any:
    v = _clean(v)
    if v in ("", "-", None):
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _date(v: Any) -> dt.date | None:
    v = _clean(v)
    if not v:
        return None
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def _ts(v: Any) -> dt.datetime | None:
    v = _clean(v)
    if not v:
        return None
    try:
        t = pd.to_datetime(v).to_pydatetime()
    except Exception:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    return t


def _tick_ts(trade_date: dt.date, clock: Any) -> dt.datetime | None:
    if not clock:
        return None
    text = str(clock)
    if len(text) > 8 and "-" in text[:10]:
        return _ts(text)
    return _ts(f"{trade_date.isoformat()} {text}")


def _json(v: Any) -> Jsonb:
    if isinstance(v, str):
        try:
            return Jsonb(json.loads(v))
        except Exception:
            return Jsonb({"value": v})
    if isinstance(v, dict):
        return Jsonb(v)
    return Jsonb({} if v is None else {"value": str(v)})


def _hash_payload(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _exchange_from_market(market: str, code: str) -> str:
    if market == "1":
        return "SSE"
    if market == "0" and (code.startswith(("8", "4")) or code.startswith("920")):
        return "BSE"
    if market == "0":
        return "SZSE"
    if market == "90":
        return "EASTMONEY_BOARD"
    return f"EASTMONEY_{market}"


def _asset_type_from_code(secid: str, explicit: str | None = None) -> str:
    """Use market+code, never a bare 000 prefix, to identify mainland securities.

    A declared type contradicting a known code family is an ingestion error.
    Unknown families retain the provider's explicit type rather than guessing.
    """
    market, code = split_secid(secid)
    known = None
    if code.startswith(("BK", "GN")):
        return explicit or "board"
    if market == "1" and code.startswith("000"):
        known = "index"
    elif market == "0" and code.startswith(("399", "899")):
        known = "index"
    elif market == "2" and code.startswith(("930", "932")):
        known = "index"
    elif (market == "0" and code.startswith(("000", "001", "002", "003", "300", "301", "4", "8", "920"))) or (market == "1" and code.startswith(("600", "601", "603", "605", "688", "689"))):
        known = "stock"
    if explicit and known and explicit != known:
        raise ValueError(f"security identity conflict: {secid} declares {explicit}, expected {known}")
    return explicit or known or "stock"


class PGStorage:
    def __init__(self, autocommit: bool = True):
        self.conn = psycopg.connect(**_connect_kwargs(), autocommit=autocommit)
        self._asset_cache: dict[tuple[str, str, str], str] = {}

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def ensure_asset(
        self,
        secid: str,
        asset_type: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        market, code = split_secid(secid)
        exchange = _exchange_from_market(market, code)
        typ = _asset_type_from_code(secid, asset_type)
        key = (typ, code, exchange)
        if key in self._asset_cache:
            return self._asset_cache[key]

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT asset_id
                FROM market.assets
                WHERE asset_type = %s
                  AND symbol = %s
                  AND COALESCE(exchange, '') = COALESCE(%s, '')
                """,
                (typ, code, exchange),
            )
            row = cur.fetchone()
            if row:
                asset_id = str(row[0])
                cur.execute(
                    """
                    UPDATE market.assets
                    SET name = COALESCE(%s, name),
                        metadata = metadata || %s,
                        updated_at = now()
                    WHERE asset_id = %s
                    """,
                    (name, Jsonb(metadata or {"eastmoney_secid": secid, "eastmoney_market": market}), asset_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO market.assets (asset_type, symbol, exchange, name, metadata)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING asset_id
                    """,
                    (typ, code, exchange, name, Jsonb(metadata or {"eastmoney_secid": secid, "eastmoney_market": market})),
                )
                asset_id = str(cur.fetchone()[0])
        self._asset_cache[key] = asset_id
        return asset_id

    def upsert_assets(self, rows: list[dict[str, Any]], asset_type: str) -> int:
        total = 0
        for r in rows:
            code_value = r.get("code")
            market_value = r.get("market")
            code = "" if code_value is None else str(code_value).strip()
            market = "" if market_value is None else str(market_value).strip()
            if not code or not market:
                continue
            secid = f"{market}.{code}"
            self.ensure_asset(
                secid,
                asset_type=asset_type,
                name=r.get("name"),
                metadata={
                    "eastmoney_secid": secid,
                    "eastmoney_market": market,
                    "is_active": True,
                    "raw": r,
                },
            )
            total += 1
        return total

    def sync_active_universe(self, asset_type: str, active_secids: list[str]) -> int:
        """Mark assets absent from a complete Eastmoney universe snapshot inactive."""
        if asset_type not in {"stock", "index"} or not active_secids:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE market.assets
                SET metadata = metadata || jsonb_build_object(
                      'is_active', false,
                      'inactive_detected_at', now()
                    ),
                    updated_at = now()
                WHERE asset_type = %s
                  AND metadata ? 'eastmoney_secid'
                  AND NOT ((metadata->>'eastmoney_secid') = ANY(%s))
                  AND coalesce((metadata->>'is_active')::boolean, true)
                """,
                (asset_type, active_secids),
            )
            return cur.rowcount

    def upsert_memberships(
        self,
        parent_secid: str,
        member_rows: list[dict[str, Any]],
        membership_type: str,
    ) -> int:
        parent_id = self.ensure_asset(parent_secid, asset_type=membership_type)
        rows = []
        for r in member_rows:
            code_value = r.get("code")
            market_value = r.get("market")
            code = "" if code_value is None else str(code_value).strip()
            market = "" if market_value is None else str(market_value).strip()
            if not code or not market:
                continue
            member_secid = f"{market}.{code}"
            member_id = self.ensure_asset(
                member_secid,
                asset_type="stock",
                name=r.get("name"),
                metadata={"eastmoney_secid": member_secid, "eastmoney_market": market, "raw": r},
            )
            rows.append((parent_id, member_id, membership_type, SOURCE, Jsonb(r)))
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.asset_memberships
                  (parent_asset_id, member_asset_id, membership_type, source, metadata, updated_at)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT (parent_asset_id, member_asset_id, membership_type, source) DO UPDATE
                SET metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                rows,
            )
        return len(rows)

    def list_instruments(self, active_only: bool = False) -> list[dict[str, Any]]:
        price_expr = """
          CASE
            WHEN COALESCE(metadata #>> '{raw,price}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (metadata #>> '{raw,price}')::numeric
            ELSE 0
          END
        """
        total_mv_expr = """
          CASE
            WHEN COALESCE(metadata #>> '{raw,total_mv}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (metadata #>> '{raw,total_mv}')::numeric
            ELSE 0
          END
        """
        active_filter = f"""
                  AND (
                    %(active_only)s = FALSE
                    OR asset_type NOT IN ('stock', 'index')
                    OR (
                      asset_type = 'stock'
                      AND ({price_expr}) > 0
                      AND ({total_mv_expr}) > 0
                    )
                    OR (
                      asset_type = 'index'
                      AND ({price_expr}) > 0
                    )
                  )
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  metadata->>'eastmoney_secid' AS secid,
                  symbol AS code,
                  name,
                  metadata->>'eastmoney_market' AS market,
                  asset_type AS type
                FROM market.assets
                WHERE metadata ? 'eastmoney_secid'
                  AND NOT (metadata ? 'identity_alias_of')
                  AND COALESCE((metadata->>'is_active')::boolean, true)
                  AND COALESCE(name, '') !~ '^退市'
                  AND NOT (
                    asset_type = 'index'
                    AND metadata->>'eastmoney_market' = '0'
                    AND symbol !~ '^399'
                  )
                  {active_filter}
                ORDER BY asset_type, symbol
                """,
                {"active_only": active_only},
            )
            return [
                {"secid": r[0], "code": r[1], "name": r[2], "market": r[3], "type": r[4]}
                for r in cur.fetchall()
                if r[0]
            ]

    def set_cursor(self, datatype: str, secid: str, cursor: Any) -> None:
        self.set_state(datatype, secid, str(cursor), "ok", None)

    def get_state(self, job: str, target: str, source: str = SOURCE) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT last_cursor, status, error_count, last_error, updated_at
                FROM market.crawl_state
                WHERE job_name = %s
                  AND target_key = %s
                  AND source = %s
                """,
                (job, target, source),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "last_cursor": row[0],
            "status": row[1],
            "error_count": row[2],
            "last_error": row[3],
            "updated_at": row[4],
        }

    def state_is_ok(
        self, job: str, target: str, cursor: str | None = None, source: str = SOURCE
    ) -> bool:
        state = self.get_state(job, target, source=source)
        if not state or state["status"] != "ok":
            return False
        if cursor is not None and state["last_cursor"] != cursor:
            return False
        return True

    def set_state(
        self,
        job: str,
        target: str,
        cursor: str | None,
        status: str,
        error: str | None,
        source: str = SOURCE,
    ) -> None:
        error_count = 0 if error is None else 1
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.crawl_state
                  (job_name, target_key, source, last_cursor, status, error_count, last_error, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (job_name, target_key, source) DO UPDATE
                SET last_cursor = EXCLUDED.last_cursor,
                    status = EXCLUDED.status,
                    error_count = CASE
                      WHEN EXCLUDED.last_error IS NULL THEN 0
                      ELSE market.crawl_state.error_count + 1
                    END,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                """,
                (job, target, source, cursor, status, error_count, error),
            )

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if table == "instruments":
            total = 0
            by_type: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                by_type.setdefault(str(r.get("type") or "stock"), []).append(r)
            for typ, group in by_type.items():
                total += self.upsert_assets(group, typ)
            return total
        if table == "valuation_snapshot":
            return self.upsert_valuation_snapshot(rows)
        if table == "financials":
            return self.upsert_financials(rows)
        if table == "events":
            return self.upsert_events(rows)
        if table == "macro":
            return self.upsert_macro(rows)
        raise ValueError(f"PGStorage does not support generic upsert table={table}")

    def write_parquet(self, datatype: str, secid: str, leaf: str, df: pd.DataFrame, dedupe_keys=None) -> int:
        if df is None or df.empty:
            return 0
        if datatype == "kline":
            return self.upsert_kline(secid, leaf, df)
        if datatype == "trends":
            return self.upsert_trends(secid, df)
        if datatype == "ticks":
            return self.upsert_ticks(secid, leaf, df)
        if datatype == "fundflow":
            return self.upsert_fundflow(secid, df)
        raise ValueError(f"PGStorage does not support datatype={datatype}")

    def upsert_kline(self, secid: str, leaf: str, df: pd.DataFrame) -> int:
        asset_id = self.ensure_asset(secid)
        klt, adjust = leaf.split("_", 1) if "_" in leaf else (leaf, "none")
        period = {"101": "day", "102": "week", "103": "month"}.get(klt, f"{klt}m")
        table = "market.bars_daily" if period == "day" else "market.bars_1m"
        source = f"eastmoney:kline:{period}:{adjust}"
        rows = []
        for _, r in df.iterrows():
            ts = _ts(r.get("date"))
            if not ts:
                continue
            rows.append((
                asset_id, ts, _num(r.get("open")), _num(r.get("high")), _num(r.get("low")),
                _num(r.get("close")), _num(r.get("volume")), _num(r.get("amount")), source,
            ))
        if not rows:
            return 0
        distinct_guard = "" if table == "market.bars_daily" else f"""
                WHERE ROW({table}.open, {table}.high, {table}.low, {table}.close, {table}.volume, {table}.amount)
                  IS DISTINCT FROM ROW(EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume, EXCLUDED.amount)
                """
        with self.conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO {table}
                  (asset_id, ts, open, high, low, close, volume, amount, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount,
                    updated_at = now()
                {distinct_guard}
                """,
                rows,
            )
        return len(rows)

    def upsert_trends(self, secid: str, df: pd.DataFrame) -> int:
        asset_id = self.ensure_asset(secid)
        rows = []
        for _, r in df.iterrows():
            ts = _ts(r.get("time"))
            if not ts:
                continue
            rows.append((
                asset_id, ts, _num(r.get("open")), _num(r.get("high")), _num(r.get("low")),
                _num(r.get("close")), _num(r.get("volume")), _num(r.get("amount")),
                "eastmoney:trends:1m",
            ))
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.bars_1m
                  (asset_id, ts, open, high, low, close, volume, amount, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount
                WHERE ROW(market.bars_1m.open, market.bars_1m.high, market.bars_1m.low, market.bars_1m.close, market.bars_1m.volume, market.bars_1m.amount)
                  IS DISTINCT FROM ROW(EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume, EXCLUDED.amount)
                """,
                rows,
            )
        return len(rows)

    def upsert_ticks(self, secid: str, leaf: str, df: pd.DataFrame) -> int:
        asset_id = self.ensure_asset(secid)
        trade_date = _date(leaf) or dt.datetime.now(TZ).date()
        rows = []
        for _, r in df.iterrows():
            ts = _tick_ts(trade_date, r.get("time"))
            if not ts:
                continue
            direction = _clean(r.get("direction"))
            rows.append((
                asset_id, trade_date, ts, _num(r.get("price")), _num(r.get("volume")),
                int(direction) if direction is not None else 0,
                "eastmoney:ticks", Jsonb({"time": str(r.get("time"))}),
            ))
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.trade_ticks
                  (asset_id, trade_date, ts, price, volume, direction, source, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, price, volume, direction, source) DO UPDATE
                SET raw_payload = EXCLUDED.raw_payload
                """,
                rows,
            )
        return len(rows)

    def upsert_fundflow(self, secid: str, df: pd.DataFrame) -> int:
        asset_id = self.ensure_asset(secid)
        rows = []
        for _, r in df.iterrows():
            d = _date(r.get("date"))
            if not d:
                continue
            rows.append((
                asset_id, d, _num(r.get("main_net")), _num(r.get("small_net")),
                _num(r.get("mid_net")), _num(r.get("big_net")), _num(r.get("super_net")),
                "eastmoney:fundflow:day", Jsonb(r.to_dict()),
            ))
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.money_flow_daily
                  (asset_id, ts, main_net, small_net, mid_net, big_net, super_net, source, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET main_net = EXCLUDED.main_net,
                    small_net = EXCLUDED.small_net,
                    mid_net = EXCLUDED.mid_net,
                    big_net = EXCLUDED.big_net,
                    super_net = EXCLUDED.super_net,
                    raw_payload = EXCLUDED.raw_payload
                WHERE ROW(
                  market.money_flow_daily.main_net,
                  market.money_flow_daily.small_net,
                  market.money_flow_daily.mid_net,
                  market.money_flow_daily.big_net,
                  market.money_flow_daily.super_net,
                  market.money_flow_daily.raw_payload
                ) IS DISTINCT FROM ROW(
                  EXCLUDED.main_net,
                  EXCLUDED.small_net,
                  EXCLUDED.mid_net,
                  EXCLUDED.big_net,
                  EXCLUDED.super_net,
                  EXCLUDED.raw_payload
                )
                """,
                rows,
            )
        return len(rows)

    def upsert_valuation_snapshot(self, rows: list[dict[str, Any]]) -> int:
        out = []
        factor_rows = []
        for r in rows:
            secid = r.get("secid")
            if not secid:
                continue
            asset_id = self.ensure_asset(secid)
            ts = _ts(r.get("dt")) or dt.datetime.now(TZ)
            out.append((
                asset_id, ts, _num(r.get("price")), _num(r.get("turnover")),
                _num(r.get("pe_ttm")), _num(r.get("pb")), _num(r.get("total_mv")),
                _num(r.get("float_mv")), "eastmoney:snapshot", Jsonb(r),
            ))
            d = ts.date()
            for key in ("price", "turnover", "pe_ttm", "pb", "total_mv", "float_mv"):
                factor_rows.append((asset_id, d, key, _num(r.get(key)), "eastmoney:snapshot", Jsonb(r)))
        with self.conn.cursor() as cur:
            if out:
                cur.executemany(
                    """
                    INSERT INTO market.quote_snapshots
                      (asset_id, ts, price, turnover, pe_ttm, pb, total_mv, float_mv, source, raw_payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (asset_id, ts, source) DO UPDATE
                    SET price = EXCLUDED.price,
                        turnover = EXCLUDED.turnover,
                        pe_ttm = EXCLUDED.pe_ttm,
                        pb = EXCLUDED.pb,
                        total_mv = EXCLUDED.total_mv,
                        float_mv = EXCLUDED.float_mv,
                        raw_payload = EXCLUDED.raw_payload
                    """,
                    out,
                )
            if factor_rows:
                cur.executemany(
                    """
                    INSERT INTO market.factors_daily
                      (asset_id, ts, factor_name, factor_value, source, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (asset_id, ts, factor_name, source) DO UPDATE
                    SET factor_value = EXCLUDED.factor_value,
                        metadata = EXCLUDED.metadata
                    """,
                    factor_rows,
                )
        return len(out)

    def upsert_quote_snapshot(self, secid: str, snapshot: dict[str, Any], raw: dict[str, Any] | None = None) -> int:
        asset_id = self.ensure_asset(secid, name=snapshot.get("name"), metadata={"eastmoney_secid": secid})
        ts = dt.datetime.now(TZ)
        factor_rows = [
            (asset_id, ts.date(), key, _num(snapshot.get(key)), "eastmoney:snapshot", Jsonb(raw or snapshot))
            for key in ("price", "turnover", "pe_ttm", "pb", "total_mv", "float_mv", "pct_chg", "vol_ratio")
        ]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.quote_snapshots
                  (asset_id, ts, price, open, high, low, pre_close, avg_price, volume, amount,
                   pct_chg, chg, amplitude, turnover, vol_ratio, pe_ttm, pb, total_mv, float_mv,
                   wei_bi, source, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET price = EXCLUDED.price,
                    raw_payload = EXCLUDED.raw_payload
                """,
                (
                    asset_id, ts, _num(snapshot.get("price")), _num(snapshot.get("open")),
                    _num(snapshot.get("high")), _num(snapshot.get("low")), _num(snapshot.get("pre_close")),
                    _num(snapshot.get("avg_price")), _num(snapshot.get("volume")), _num(snapshot.get("amount")),
                    _num(snapshot.get("pct_chg")), _num(snapshot.get("chg")), _num(snapshot.get("amplitude")),
                    _num(snapshot.get("turnover")), _num(snapshot.get("vol_ratio")), _num(snapshot.get("pe_ttm")),
                    _num(snapshot.get("pb")), _num(snapshot.get("total_mv")), _num(snapshot.get("float_mv")),
                    _num(snapshot.get("wei_bi")), "eastmoney:snapshot", Jsonb(raw or snapshot),
                ),
            )
            cur.executemany(
                """
                INSERT INTO market.factors_daily
                  (asset_id, ts, factor_name, factor_value, source, metadata)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, factor_name, source) DO UPDATE
                SET factor_value = EXCLUDED.factor_value,
                    metadata = EXCLUDED.metadata
                """,
                factor_rows,
            )
        return 1

    def upsert_order_book(self, secid: str, payload: dict[str, Any]) -> int:
        asset_id = self.ensure_asset(secid)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market.order_book_snapshots
                  (asset_id, ts, wei_bi, wei_cha, source, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET raw_payload = EXCLUDED.raw_payload,
                    wei_bi = EXCLUDED.wei_bi,
                    wei_cha = EXCLUDED.wei_cha
                """,
                (
                    asset_id,
                    dt.datetime.now(TZ),
                    _num(payload.get("wei_bi") or payload.get("f191")),
                    _num(payload.get("wei_cha") or payload.get("f192")),
                    "eastmoney:orderbook",
                    Jsonb(payload),
                ),
            )
        return 1

    def upsert_financials(self, rows: list[dict[str, Any]]) -> int:
        out = []
        for r in rows:
            secid = r.get("secid")
            d = _date(r.get("report_date"))
            if not secid or not d:
                continue
            out.append((
                self.ensure_asset(secid, asset_type="stock"), d, r.get("item"),
                _num(r.get("value")), "eastmoney:fundamental", Jsonb(r),
            ))
        if not out:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO company.financial_metrics
                  (asset_id, report_date, metric_name, metric_value, source, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, report_date, metric_name, source) DO UPDATE
                SET metric_value = EXCLUDED.metric_value,
                    raw_payload = EXCLUDED.raw_payload
                """,
                out,
            )
        return len(out)

    def upsert_events(self, rows: list[dict[str, Any]]) -> int:
        out = []
        for r in rows:
            secid = r.get("secid")
            payload = r.get("payload")
            out.append((
                r.get("category"),
                self.ensure_asset(secid) if secid else None,
                _date(r.get("trade_date")),
                "eastmoney:datacenter",
                _hash_payload(payload),
                _json(payload),
            ))
        if not out:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.events_raw
                  (category, asset_id, event_date, source, source_hash, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                out,
            )
        return len(out)

    def upsert_macro(self, rows: list[dict[str, Any]]) -> int:
        primary_value_keys = {
            "CPI": "NATIONAL_SAME",
            "PPI": "BASE_SAME",
            "GDP": "SUM_SAME",
            "PMI": "MAKE_INDEX",
            "MONEY_SUPPLY": "BASIC_CURRENCY_SAME",
            "SOCIAL_FINANCE": "TOTAL_SAME",
            "LPR": "LPR1Y",
            "DEPOSIT_RATE": "DEPOSIT_RATE_BA",
            "FX_RESERVE": "GOLD_RESERVES",
            "INDUSTRY_VALUE_ADD": "BASE_SAME",
        }
        total = 0
        with self.conn.cursor() as cur:
            for r in rows:
                code = str(r.get("indicator") or "").strip()
                if not code:
                    continue
                cur.execute(
                    """
                    INSERT INTO macro.indicators (indicator_code, source)
                    VALUES (%s, 'eastmoney')
                    ON CONFLICT (indicator_code) DO UPDATE
                    SET updated_at = now()
                    RETURNING indicator_id
                    """,
                    (code,),
                )
                indicator_id = cur.fetchone()[0]
                period = str(r.get("period") or "")
                raw_payload = r.get("payload")
                if isinstance(raw_payload, str):
                    try:
                        payload_data = json.loads(raw_payload)
                    except Exception:
                        payload_data = {"value": raw_payload}
                elif isinstance(raw_payload, dict):
                    payload_data = raw_payload
                else:
                    payload_data = {}
                payload = _json(payload_data)
                primary_key = primary_value_keys.get(code)
                primary_value = payload_data.get(primary_key) if primary_key else None
                cur.execute(
                    """
                    INSERT INTO macro.indicator_values
                      (indicator_id, period, observed_at, value, source, raw_payload)
                    VALUES (%s,%s,%s,%s,'eastmoney',%s)
                    ON CONFLICT (indicator_id, period, source) DO UPDATE
                    SET observed_at = EXCLUDED.observed_at,
                        value = EXCLUDED.value,
                        raw_payload = EXCLUDED.raw_payload
                    """,
                    (indicator_id, period, _date(period), primary_value, payload),
                )
                total += 1
        return total

    def insert_source_fetch(
        self,
        source_name: str,
        external_id: str | None,
        url: str,
        http_status: int | None,
        raw_payload: dict[str, Any] | None = None,
        raw_text: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO corpus.source_fetches
                  (source_name, external_id, url, http_status, raw_payload, raw_text, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (source_name, external_id, url, http_status, Jsonb(raw_payload or {}), raw_text, error),
            )

    def upsert_news_article(self, article: dict[str, Any]) -> str:
        source_name = article.get("source_name") or "eastmoney_news"
        external_id = article.get("external_id") or _hash_payload(article.get("url") or article.get("title"))
        published_at = _ts(article.get("published_at"))
        content = article.get("content_text")
        content_hash = _hash_payload(content or article.get("url") or external_id)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO corpus.documents
                  (source_type, source_name, external_id, title, author, publisher,
                   published_at, url, content_text, raw_text, metadata, content_hash, ingest_status)
                VALUES ('news', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ok')
                ON CONFLICT (source_type, source_name, external_id) DO UPDATE
                SET title = EXCLUDED.title,
                    author = EXCLUDED.author,
                    publisher = EXCLUDED.publisher,
                    published_at = EXCLUDED.published_at,
                    url = EXCLUDED.url,
                    content_text = EXCLUDED.content_text,
                    raw_text = EXCLUDED.raw_text,
                    metadata = corpus.documents.metadata || EXCLUDED.metadata,
                    content_hash = EXCLUDED.content_hash,
                    ingest_status = 'ok',
                    updated_at = now()
                RETURNING id
                """,
                (
                    source_name,
                    external_id,
                    article.get("title"),
                    article.get("author"),
                    article.get("publisher") or "东方财富",
                    published_at,
                    article.get("url"),
                    content,
                    article.get("raw_text"),
                    Jsonb(article.get("metadata") or {}),
                    content_hash,
                ),
            )
            doc_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO news.articles
                  (document_id, source_name, newspaper_date, author, topic, metadata)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (document_id) DO UPDATE
                SET newspaper_date = EXCLUDED.newspaper_date,
                    author = EXCLUDED.author,
                    topic = EXCLUDED.topic,
                    metadata = news.articles.metadata || EXCLUDED.metadata
                """,
                (
                    doc_id,
                    source_name,
                    published_at.date() if published_at else None,
                    article.get("author"),
                    article.get("topic"),
                    Jsonb(article.get("metadata") or {}),
                ),
            )
        return str(doc_id)
