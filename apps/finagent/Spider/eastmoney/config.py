# -*- coding: utf-8 -*-
"""东方财富爬虫集中配置。

沿用 YiLinSpider/config.py 的分类配置风格：网络/存储/标的/接口字段表
均在此模块顶层实例化，其他模块直接 `from config import systemConfig` 使用。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class SystemConfig:
    """网络层：主机回退链、token、UA、限速、重试、并发。"""

    # 东财公开行情 token（quote.eastmoney.com 前端长期使用的固定 ut）
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    UT_DETAILS = "bd1d9ddb04089700cf9c27f6f7426281"

    # /api/qt/* 全部端点（kline/fflow/stock/get/trends2/details/clist）走
    # 同一回退链：优先 push2（实时）→ push2his（历史镜像）→ push2delay
    # （约15分钟延迟，但镜像完整 API 且对 python-requests 最稳定，保底）。
    # 客户端按序探测、缓存首个可用主机；本环境前两者被 WAF/TLS 拦截，
    # 自动落到 push2delay。
    QT_HOSTS = [
        "push2.eastmoney.com",
        "push2his.eastmoney.com",
        "push2delay.eastmoney.com",
    ]
    HOST_DATACENTER = "datacenter-web.eastmoney.com"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://quote.eastmoney.com/",
    }

    TIMEOUT = 15
    MAX_RETRIES = 3
    # 单次请求间隔（秒）。东财对单 IP 限速较严，过快会被临时封禁导致
    # 所有接口返回空 klines/diff；0.6s + 并发6 较稳，被封时调大本值。
    REQUEST_INTERVAL = 0.6
    MAX_WORKERS = 6              # ThreadPoolExecutor 并发数
    HOST_PROBE_TIMEOUT = 6       # 探测 push2 是否可达的超时


class StorageConfig:
    """存储层：SQLite + Parquet 分区目录，自建目录。"""

    def __init__(self, base_path=os.path.join(BASE_DIR, "data")):
        self.BASE_PATH = base_path
        self.DB_PATH = os.path.join(base_path, "eastmoney.db")
        self.PARQUET_DIR = os.path.join(base_path, "parquet")
        for sub in ("", "parquet"):
            os.makedirs(os.path.join(base_path, sub), exist_ok=True)

    def parquet_path(self, datatype, secid, leaf):
        """parquet/{datatype}/{secid}/{leaf}.parquet"""
        d = os.path.join(self.PARQUET_DIR, datatype, secid.replace(".", "_"))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{leaf}.parquet")


class UniverseConfig:
    """采集标的池。先跑通 sh600909 + 沪深指数，可由 init_universe 扩展。"""

    # 默认个股清单（secid 形式，market.code）
    DEFAULT_STOCKS = ["1.600909"]

    # 沪深指数列表的 clist 过滤串（实测返回 43 个指数）
    INDEX_LIST_FS = "b:MK0010"

    # 沪深京 A 股全量列表 clist 过滤串（实测 5500+ 只）
    ALL_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    ALL_A_FS_GROUPS = [
        "m:0+t:6",
        "m:0+t:80",
        "m:1+t:2",
        "m:1+t:23",
        "m:0+t:81+s:2048",
    ]

    # 行业 / 概念板块
    INDUSTRY_FS = "m:90+t:2+f:!50"
    CONCEPT_FS = "m:90+t:3+f:!50"


class ApiConfig:
    """接口字段表 / K线周期 / 复权 / 宏观 reportName 表。"""

    # klt: K线周期。1/5/15/30/60 分钟，101 日，102 周，103 月
    KLT = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
        "day": 101, "week": 102, "month": 103,
    }
    # fqt: 复权。0 不复权，1 前复权，2 后复权
    FQT = {"none": 0, "qfq": 1, "hfq": 2}

    # 默认采集组合（周期, 复权）
    KLINE_TASKS = [("day", "qfq"), ("day", "none"),
                   ("60m", "qfq"), ("5m", "qfq")]

    # ---- 接口字段串 ----
    KLINE_F1 = "f1,f2,f3,f4,f5,f6"
    KLINE_F2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    KLINE_COLS = ["date", "open", "close", "high", "low",
                  "volume", "amount", "amplitude", "pct_chg",
                  "chg", "turnover"]

    TRENDS_F1 = "f1,f2,f3,f7,f8,f9,f10,f11,f12,f13"
    TRENDS_F2 = "f51,f52,f53,f54,f55,f56,f57,f58"
    TRENDS_COLS = ["time", "open", "close", "high", "low",
                   "volume", "amount", "avg_price"]

    DETAILS_F1 = "f1,f2,f3"
    DETAILS_F2 = "f51,f52,f53,f55"
    DETAILS_COLS = ["time", "price", "volume", "direction"]
    # 逐笔单次返回上限，pos 负偏移翻页；当日最多约 6000+ 笔
    DETAILS_PAGE = 4000

    FFLOW_F1 = "f1,f2,f3,f7"
    FFLOW_F2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    FFLOW_COLS = ["date", "main_net", "small_net", "mid_net",
                  "big_net", "super_net"]

    # 快照字段：现价/最高/最低/今开/量/额/代码/名称/昨收/总市值/流通市值/
    # PE动/PB/换手/涨跌额/涨跌幅/小数位/量比/振幅/委比
    SNAPSHOT_FIELDS = (
        "f43,f44,f45,f46,f47,f48,f50,f57,f58,f59,f60,f71,"
        "f116,f117,f162,f167,f168,f169,f170,f171,f191"
    )
    SNAPSHOT_MAP = {
        "f43": "price", "f44": "high", "f45": "low", "f46": "open",
        "f47": "volume", "f48": "amount", "f50": "vol_ratio",
        "f57": "code", "f58": "name", "f59": "decimal",
        "f60": "pre_close", "f71": "avg_price",
        "f116": "total_mv", "f117": "float_mv", "f162": "pe_ttm",
        "f167": "pb", "f168": "turnover", "f169": "chg",
        "f170": "pct_chg", "f171": "amplitude", "f191": "wei_bi",
    }
    # 需按 f59 小数位 /10^decimal 缩放的价格类字段
    SNAPSHOT_SCALED = {"f43", "f44", "f45", "f46", "f60", "f71", "f169"}

    CLIST_FIELDS = "f12,f13,f14,f2,f3,f4,f20,f21,f8,f9,f23"
    CLIST_MAP = {
        "f12": "code", "f13": "market", "f14": "name", "f2": "price",
        "f3": "pct_chg", "f4": "chg", "f20": "total_mv",
        "f21": "float_mv", "f8": "turnover", "f9": "pe", "f23": "pb",
    }

    # 宏观：datacenter reportName 表驱动（实测 RPT_ECONOMY_CPI 可用）
    MACRO_REPORTS = {
        "CPI": "RPT_ECONOMY_CPI",
        "PPI": "RPT_ECONOMY_PPI",
        "GDP": "RPT_ECONOMY_GDP",
        "PMI": "RPT_ECONOMY_PMI",
        "MONEY_SUPPLY": "RPT_ECONOMY_CURRENCY_SUPPLY",
        "SOCIAL_FINANCE": "RPT_ECONOMY_SOCIAL_FINANCING",
        "LPR": "RPT_ECONOMY_LPR",
        "DEPOSIT_RATE": "RPT_ECONOMY_DEPOSIT_RATE",
        "FX_RESERVE": "RPT_ECONOMY_FOREX",
        "INDUSTRY_VALUE_ADD": "RPT_ECONOMY_GYZJZ",
    }

    # 事件类 datacenter reportName 表
    EVENT_REPORTS = {
        "dragon_tiger": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "block_trade": "RPT_DATA_BLOCKTRADE",
        "margin_trade": "RPT_RZRQ_LSHJMX",
        "unlock": "RPT_LIFTING_DETAIL",
        "survey": "RPT_ORG_SURVEYNEW",
        "northbound": "RPT_MUTUAL_DEAL_HISTORY",
    }


systemConfig = SystemConfig()
storageConfig = StorageConfig()
universeConfig = UniverseConfig()
apiConfig = ApiConfig()
