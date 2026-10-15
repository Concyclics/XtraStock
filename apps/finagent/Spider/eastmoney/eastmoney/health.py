"""Small representative probes; failures never imply complete source data."""
from eastmoney.fetchers import kline, snapshot, trends, fundflow
import datetime as dt


def probe_market_feeds():
    since = (dt.date.today() - dt.timedelta(days=20)).strftime("%Y%m%d")
    probes = {
        "daily": lambda code: not kline.fetch_kline(code, period="day", adjust="qfq", beg=since, end="20500101").empty
            and not kline.fetch_kline(code, period="day", adjust="none", beg=since, end="20500101").empty,
        "snapshot": lambda code: bool((snapshot.fetch_snapshot(code) or {}).get("price")),
        "trends": lambda code: not trends.fetch_trends(code, ndays=1).empty,
        "fundflow": lambda code: not fundflow.fetch_fundflow(code, lmt=5).empty,
    }
    result = {}
    for name, probe in probes.items():
        result[name] = False
        for code in ("1.600519", "0.000001"):
            try:
                if probe(code):
                    result[name] = True
                    break
            except (ValueError, TypeError, KeyError):
                continue
    return result
