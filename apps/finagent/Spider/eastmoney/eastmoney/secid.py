# -*- coding: utf-8 -*-
"""标的代码 <-> 东财 secid 互转，及标的池加载。

secid = "{market}.{code}"：
  沪市(60/68/9/5/110/113/132/204 及上证指数 000) -> 1
  深市(00/30/12/15/16/18/20/39) / 北交所(8/4/920) -> 0
  港股 -> 116，美股 -> 105/106/107（暂按 105）
指数：上证系列 000xxx -> 1.000xxx；深证系列 399xxx -> 0.399xxx
"""
import re

_SH_PREFIX = ("60", "68", "11", "13", "5", "9", "204")
_SH_INDEX = ("000",)          # 000001 上证指数 / 000300 沪深300 ...
_SZ_INDEX = ("399",)          # 399001 深证成指 / 399006 创业板指 ...


def to_secid(code, market=None):
    """'sh600909' / '600909' / '600909.SH' / 'sz000001' -> '1.600909'。"""
    code = str(code).strip().lower()
    m = re.match(r"^(sh|sz|bj)?\.?(\d{6})(\.(sh|sz|bj))?$", code)
    if not m:
        if re.match(r"^[01]\.\d{6}$", code):  # 已是 secid
            return code
        raise ValueError(f"无法解析代码: {code}")
    prefix = m.group(1) or m.group(4)
    digits = m.group(2)
    if market in ("1", "0"):
        return f"{market}.{digits}"
    if prefix == "sh":
        return f"1.{digits}"
    if prefix in ("sz", "bj"):
        return f"0.{digits}"
    # 无前缀：按数字规则推断
    if digits.startswith(_SH_INDEX) or digits.startswith(_SH_PREFIX):
        return f"1.{digits}"
    if digits.startswith(_SZ_INDEX) or digits[0] in ("0", "1", "2", "3"):
        return f"0.{digits}"
    if digits[0] in ("8", "4") or digits.startswith("920"):
        return f"0.{digits}"
    raise ValueError(f"无法推断市场: {code}")


def split_secid(secid):
    """'1.600909' -> ('1', '600909')"""
    market, code = secid.split(".")
    return market, code


def load_universe():
    """从 SQLite instruments 表读全部 secid；为空则回退 config 默认清单。"""
    from config import universeConfig
    try:
        from eastmoney.storage import Storage
        rows = Storage().list_instruments()
        if rows:
            return [r["secid"] for r in rows]
    except Exception:
        pass
    return list(universeConfig.DEFAULT_STOCKS)
