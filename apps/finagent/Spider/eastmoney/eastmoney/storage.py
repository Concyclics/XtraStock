# -*- coding: utf-8 -*-
"""持久化层：SQLite（元数据/小表/事件）+ Parquet（大时序表）。

- SQLite：关系查询友好，含 crawl_log 续采游标（参照 openreview-spider
  的 existed 集合 + DB 游标思路）。
- Parquet：K线/分时/逐笔/资金流大表，pandas 直读高效；写入按去重键
  upsert（读出旧表 -> concat -> drop_duplicates -> 重写）。
"""
import os
import sqlite3
import pandas as pd

from config import storageConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
  secid TEXT PRIMARY KEY, code TEXT, name TEXT,
  market TEXT, type TEXT, board TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS index_constituents (
  index_secid TEXT, member_secid TEXT, member_code TEXT,
  member_name TEXT, weight REAL, updated_at TEXT,
  PRIMARY KEY (index_secid, member_secid));
CREATE TABLE IF NOT EXISTS financials (
  secid TEXT, report_date TEXT, item TEXT, value REAL,
  PRIMARY KEY (secid, report_date, item));
CREATE TABLE IF NOT EXISTS valuation_snapshot (
  secid TEXT, dt TEXT, pe_ttm REAL, pb REAL, total_mv REAL,
  float_mv REAL, turnover REAL, price REAL,
  PRIMARY KEY (secid, dt));
CREATE TABLE IF NOT EXISTS events (
  category TEXT, secid TEXT, trade_date TEXT, payload TEXT,
  PRIMARY KEY (category, secid, trade_date, payload));
CREATE TABLE IF NOT EXISTS macro (
  indicator TEXT, period TEXT, payload TEXT,
  PRIMARY KEY (indicator, period));
CREATE TABLE IF NOT EXISTS crawl_log (
  datatype TEXT, secid TEXT, last_cursor TEXT, updated_at TEXT,
  PRIMARY KEY (datatype, secid));
"""


class Storage:
    def __init__(self, db_path=None):
        self.db_path = db_path or storageConfig.DB_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- SQLite 通用 upsert ----
    def upsert(self, table, rows):
        if not rows:
            return 0
        cols = list(rows[0].keys())
        ph = ",".join("?" * len(cols))
        sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
               f"VALUES ({ph})")
        self.conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        self.conn.commit()
        return len(rows)

    def list_instruments(self):
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM instruments")]

    # ---- 断点续采游标 ----
    def get_cursor(self, datatype, secid):
        r = self.conn.execute(
            "SELECT last_cursor FROM crawl_log WHERE datatype=? AND secid=?",
            (datatype, secid)).fetchone()
        return r["last_cursor"] if r else None

    def set_cursor(self, datatype, secid, cursor):
        self.conn.execute(
            "INSERT OR REPLACE INTO crawl_log "
            "(datatype, secid, last_cursor, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (datatype, secid, str(cursor)))
        self.conn.commit()

    # ---- Parquet 时序表 upsert ----
    def write_parquet(self, datatype, secid, leaf, df, dedupe_keys):
        if df is None or df.empty:
            return 0
        path = storageConfig.parquet_path(datatype, secid, leaf)
        if os.path.exists(path):
            old = pd.read_parquet(path)
            df = pd.concat([old, df], ignore_index=True)
        df = (df.drop_duplicates(subset=dedupe_keys, keep="last")
                .sort_values(dedupe_keys)
                .reset_index(drop=True))
        df.to_parquet(path, index=False)
        return len(df)

    def close(self):
        self.conn.close()
