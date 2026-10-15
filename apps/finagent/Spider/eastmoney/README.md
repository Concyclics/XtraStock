# 东方财富爬虫 (eastmoney)

为回测与量化策略设计采集东方财富的**细粒度行情 + 资金流 + 基本面 +
事件 + 宏观**数据。覆盖个股与指数，标的可配置。

## 安装

```bash
pip install -r requirements.txt   # requests pandas pyarrow tqdm beautifulsoup4
```

Python 3.10+。

## 快速开始

```bash
# 1. 拉取标的池（指数/全A/行业/概念）-> SQLite instruments
python scripts/init_universe.py

# 2. 历史回填（K线/资金流/财务/宏观，可全历史回溯）
python scripts/crawl_history.py --secid 1.600909 --macro
python scripts/crawl_history.py --universe          # 全标的池

# 3. 每日增量（收盘后运行，累积分时/逐笔/快照/当日K线/事件）
python scripts/crawl_daily.py --secid 1.600909

# 单个 fetcher 自测
python -m eastmoney.fetchers.kline 1.600909
python -m eastmoney.fetchers.macro CPI
```

`secid = "{market}.{code}"`：沪市/上证指数=`1`，深市/北交所=`0`。
如 `sh600909 -> 1.600909`，沪深300 -> `1.000300`，深证成指 -> `0.399001`。
`eastmoney.secid.to_secid()` 支持 `sh600909 / 600909 / 600909.SH` 等写法。

## 接口与主机回退

页面（`quote.eastmoney.com/...`）只是壳，数据来自公开 JSON 接口：

| 数据 | 接口 |
|---|---|
| 多周期K线 | `/api/qt/stock/kline/get` |
| 分时 | `/api/qt/stock/trends2/get` |
| 逐笔成交 | `/api/qt/stock/details/get` |
| 实时快照 | `/api/qt/stock/get` |
| 历史资金流 | `/api/qt/stock/fflow/daykline/get` |
| 标的/板块列表 | `/api/qt/clist/get` |
| 财务/事件/宏观 | `datacenter-web /api/data/v1/get` (reportName 切换) |

`/api/qt/*` 主机回退链（`config.SystemConfig.QT_HOSTS`）：
`push2`（实时，最佳）→ `push2his`（历史镜像）→ `push2delay`（约15分钟
延迟，但镜像完整 API 且对 python-requests 最稳定，保底）。客户端按序
探测、缓存首个可用主机；某些网络下 push2/push2his 会被 WAF/TLS 拦截，
自动落到 push2delay（对历史回测无影响，回测本就用历史数据）。

## 数据落地

- **SQLite** `data/eastmoney.db`：`instruments` 标的池、`index_constituents`
  指数成分、`financials` 财务、`valuation_snapshot` 估值时序、`events`
  事件（龙虎榜/大宗/两融/解禁/调研/北向，整行 JSON）、`macro` 宏观、
  `crawl_log` 断点续采游标。
- **Parquet** `data/parquet/{datatype}/{secid}/...`：`kline/{klt}_{fqt}`、
  `trends/{date}`、`ticks/{date}`、`fundflow/day`。按去重键 upsert，
  pandas `read_parquet` 直读用于回测。

### Postgres 直写

当前项目也支持直接写入 `finance_corpus` Docker 数据库。连接信息默认读取
仓库根目录 `.env.finance-db`，端口为 `127.0.0.1:15432`。

```bash
# 建表，执行一次即可
.venv/bin/python - <<'PY'
from pathlib import Path
import psycopg

env = {}
for line in Path(".env.finance-db").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v

sql = Path("docker/postgres/schema_eastmoney_market.sql").read_text()
conn = psycopg.connect(
    host="127.0.0.1", port=15432,
    dbname=env["POSTGRES_DB"],
    user=env["POSTGRES_USER"],
    password=env["POSTGRES_PASSWORD"],
)
with conn:
    conn.execute(sql)
PY

# 标的池：指数、股票、行业、概念 -> market.assets
.venv/bin/python Spider/eastmoney/scripts/init_universe_pg.py

# 行业/概念成分关系 -> market.asset_memberships
.venv/bin/python Spider/eastmoney/scripts/crawl_memberships_pg.py --asset-type industry --asset-type concept

# 历史回填：K线、资金流、财务、事件、宏观
.venv/bin/python Spider/eastmoney/scripts/crawl_history_pg.py --secid 1.600909 --macro
.venv/bin/python Spider/eastmoney/scripts/crawl_history_pg.py --universe --asset-type stock --no-events

# 每日增量：日K/5m、快照、分时、逐笔、盘口、事件、新闻
.venv/bin/python Spider/eastmoney/scripts/crawl_daily_pg.py --secid 1.600909 --news
.venv/bin/python Spider/eastmoney/scripts/crawl_daily_pg.py --universe --asset-type stock

# 新闻单独跑
.venv/bin/python Spider/eastmoney/scripts/crawl_news_pg.py --channel finance --limit 50
```

Postgres 表映射：

| 数据 | 表 |
|---|---|
| 标的 | `market.assets` |
| 指数/行业/概念成分关系 | `market.asset_memberships` |
| 日 K | `market.bars_daily` |
| 5m/60m/分时 | `market.bars_1m` |
| 快照/估值/市值 | `market.quote_snapshots`, `market.factors_daily` |
| 逐笔成交 | `market.trade_ticks` |
| 盘口 raw | `market.order_book_snapshots` |
| 资金流 | `market.money_flow_daily` |
| 财务指标 | `company.financial_metrics` |
| 龙虎榜/大宗/两融/调研/北向 raw | `market.events_raw` |
| 宏观 | `macro.indicators`, `macro.indicator_values` |
| 东方财富新闻 | `corpus.documents`, `news.articles`, `corpus.source_fetches` |
| 增量状态 | `market.crawl_state` |

`init_universe_pg.py` 按沪市、科创、深市、创业、北交分组拉取并去重。
当前东方财富公开 `clist` 接口对这些分组返回约 3300 个股票标的；如需交易所
官网口径的完整全 A 清单，应再接入交易所证券列表作为补充源。

## 字段字典

K线列：`date,open,close,high,low,volume,amount,amplitude,pct_chg,chg,
turnover`；分时：`time,open,close,high,low,volume,amount,avg_price`；
逐笔：`time,price,volume,direction(1买/2卖/4集合竞价)`；资金流：`date,
main_net,small_net,mid_net,big_net,super_net`。完整 fxx 字段码对照见
`eastmoney/catalog.py` 与 `config.ApiConfig`。

## 可用于量化的维度

- **择时/选股因子**：多周期 OHLCV、换手率、振幅、资金流（主力/超大单
  净流入）、北向资金、估值时序（PE-TTM/PB）。
- **微观结构**：分时均价偏离、逐笔买卖单不平衡（须每日累积，不可回填）。
- **基本面**：营收/净利/EPS/ROE/毛利率/资产负债率及同比。
- **事件风险**：龙虎榜席位、大宗折溢价、两融余额、限售解禁、机构调研。
- **自上而下**：CPI/PPI/GDP/PMI/M2/社融/LPR/汇率宏观择时。

## 注意

- **逐笔与分时只能从采集日起向前累积，无法回填历史**：需每个交易日
  收盘后跑 `crawl_daily.py`。K线/资金流/财务/宏观可全历史回溯。
- 限速：`config.SystemConfig.REQUEST_INTERVAL/MAX_WORKERS`，过快会被
  临时封禁导致接口返回空，调大间隔即可恢复。
