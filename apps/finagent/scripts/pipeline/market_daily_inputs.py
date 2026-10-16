"""Cross-industry, point-in-time evidence for the market daily report.

Selection is over the covered A-share universe, never an AI keyword whitelist.
All statistics are computed here; the language model only explains evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import json
from pathlib import Path
import re


def load_config(path=None):
    path = path or Path(__file__).resolve().parents[2] / 'config/market_daily.json'
    cfg = json.loads(Path(path).read_text())
    if cfg['version'] != 'market-daily-v1' or cfg['scope'] != 'all_a_share':
        raise ValueError('unsupported market daily scope')
    for key, maximum in {'news_days':14, 'report_days':30, 'document_limit':60,
                         'report_limit':60, 'hot_sector_limit':12, 'valuation_limit':12,
                         'watchlist_limit':20, 'max_per_industry':4}.items():
        if type(cfg[key]) is not int or not 1 <= cfg[key] <= maximum:
            raise ValueError('invalid daily evidence bound: ' + key)
    for key in ('market_relevance_pattern','routine_disclosure_pattern'):
        if not isinstance(cfg[key],str) or not 1<=len(cfg[key])<=2000:
            raise ValueError('invalid editorial ranking pattern: '+key)
        re.compile(cfg[key])
    return cfg


def diversify(rows, key, limit, per_group=2):
    """Round-robin pre-ranked groups, keeping order inside each industry/source.

    Unknown sectors share a bucket. They do not masquerade as diverse sectors.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row.get(key) or '未分类'].append(row)
    result = []
    for rank in range(per_group):
        for items in groups.values():
            if rank < len(items):
                result.append(items[rank])
                if len(result) >= limit:
                    return result
    return result


def load_valuation_screen(conn, cutoff, market_date, cfg):
    from scripts.analytics.run_daily_value_plan import EARNINGS_SQL
    from scripts.analytics.value_plan import load_config as value_config, balanced_consensus, valuations

    policy = value_config()
    day = cutoff.date()
    params = {'since': day-dt.timedelta(days=policy['forecast_max_age_days']),
              'day': day, 'cutoff': cutoff, 'fiscal_year': day.year, 'market_date': market_date}
    observations = conn.execute(EARNINGS_SQL, params).fetchall()
    by_code, actions = defaultdict(list), defaultdict(list)
    for row in observations:
        by_code[row['stock_code']].append(row)
    for row in conn.execute('''SELECT a.symbol stock_code,c.ex_date
        FROM market.corporate_actions c JOIN market.assets a USING(asset_id)
        WHERE c.ex_date BETWEEN %(since)s AND %(day)s AND c.available_at<=%(cutoff)s
          AND c.quality_status<>'rejected' AND (coalesce(c.split_ratio,0)<>0
          OR coalesce(c.stock_dividend_ratio,0)<>0 OR coalesce(c.rights_ratio,0)<>0
          OR c.action_type IN ('split','reverse_split','rights_issue'))''', params):
        actions[row['stock_code']].append({**row, 'changes_share_basis':True})
    assets = conn.execute('''WITH industry AS MATERIALIZED (
        SELECT DISTINCT ON(stock_code) stock_code,industry_name
        FROM research.reports_resolved WHERE nullif(industry_name,'') IS NOT NULL
          AND report_date BETWEEN %(since)s AND %(day)s
          AND greatest(available_at,created_at,updated_at,identity_known_at)<=%(cutoff)s
        ORDER BY stock_code,report_date DESC,document_id
      ) SELECT a.symbol stock_code,a.name,i.industry_name industry,
          b.close,b.quality_status,b.price_basis,b.available_at,b.dt market_date,
          life.lifecycle_status,life.is_st,life.is_suspended,life.is_tradable
        FROM market.assets a LEFT JOIN market.canonical_bars_daily_v2 b
          ON b.asset_id=a.asset_id AND b.dt=%(market_date)s
          AND greatest(b.available_at,b.updated_at)<=%(cutoff)s
          AND b.quality_status NOT IN ('rejected','conflict','incomplete')
        LEFT JOIN industry i ON i.stock_code=a.symbol
        LEFT JOIN LATERAL (SELECT lifecycle_status,is_st,is_suspended,is_tradable
          FROM market.asset_lifecycle_versions l WHERE l.asset_id=a.asset_id
            AND l.valid_from<=%(market_date)s AND (l.valid_to IS NULL OR l.valid_to>%(market_date)s)
            AND l.available_at<=%(cutoff)s AND l.quality_status<>'rejected'
          ORDER BY l.valid_from DESC,l.available_at DESC,l.lifecycle_version_id DESC LIMIT 1) life ON true
        WHERE a.asset_type='stock' AND a.exchange IN ('SSE','SZSE','BSE')
          AND NOT(a.metadata ? 'identity_alias_of') AND a.created_at<=%(cutoff)s
        ORDER BY a.symbol''', params).fetchall()
    inputs = []
    for asset in assets:
        if asset.get('is_st') or asset.get('is_suspended') or asset.get('is_tradable') is False:
            continue
        code = asset['stock_code']
        dated = [r for r in by_code[code] if (r.get('evidence') or {}).get('industry')]
        industry = asset['industry'] or (max(dated, key=lambda r:(r['report_date'],r['known_at']))['evidence']['industry'] if dated else None)
        inputs.append({**asset, 'industry':industry, 'consensus':balanced_consensus(
            by_code[code], cutoff, day.year, policy['forecast_max_age_days'], actions[code])})
    valued = valuations(inputs, policy)
    candidates = []
    for row in valued:
        if not row['forward_pe'] or row['forward_pe'] <= 0:
            continue
        c = row['consensus']
        scenario = row['valuation_scenarios'] or {}
        discount = (1-row['close']/row['fair_value'])*100 if row['fair_value'] else None
        gaps = list(row['valuation_gaps'])
        if row['quality_status'] != 'verified':
            gaps.append('收盘价仍为单来源行情')
        supported = not gaps and discount is not None and discount > 0
        candidates.append({
            'stock_code':row['stock_code'], 'name':row['name'], 'industry':row['industry'],
            'close':round(row['close'],2), 'price_basis':row['price_basis'],
            'market_date':market_date, 'forecast_year':c['fiscal_year'],
            'eps_median':round(c['eps_median'],4), 'institutions':c['institutions'],
            'forward_pe':round(row['forward_pe'],2),
            'peer_pe':round(scenario['pe_multiple'],2) if scenario else None,
            'peer_count':scenario.get('peer_count'),
            'peer_discount_percent':round(discount,2) if discount is not None else None,
            'screen_label':'同业折价线索' if supported else '估值观察',
            'supported_discount':supported, 'gaps':gaps,
            'meaning':'当前财年预测市盈率；同业折价是相对估值差，不是未来涨幅。',
            'prediction_eligible':False,
            'available_at':max([cutoff if row['available_at'] is None else row['available_at']]
                               + [r['known_at'] for r in c['evidence']]),
            'sources':[{'institution':r['institution'], 'report_date':r['report_date'],
                        'known_at':r['known_at'], 'source_url':(r.get('evidence') or {}).get('source_url'),
                        'document_id':str(r.get('document_id') or ''), 'eps':r['value'],
                        'source_basis':r['source_basis']} for r in c['evidence']],
        })
    candidates.sort(key=lambda r:(not r['supported_discount'],
        r['institutions'] < policy['min_institutions'],
        -(r['peer_discount_percent'] or 0) if r['supported_discount'] else r['forward_pe'], r['stock_code']))
    selected = diversify(candidates, 'industry', cfg['valuation_limit'], cfg['max_per_industry'])
    return selected, {
        'universe':'数据库已覆盖的A股证券；不代表全部上市股票均有行情或盈利预测',
        'covered_stocks':len(assets), 'priced_stocks':sum(bool(r['close']) for r in assets),
        'positive_eps_with_price':len(candidates),
        'supported_peer_discount_stocks':sum(r['supported_discount'] for r in candidates),
        'selected_valuation_stocks':len(selected), 'market_date':market_date,
        'forecast_year':day.year, 'forecast_max_age_days':policy['forecast_max_age_days'],
        'valuation_method':'每机构取同财年最新预测，再取机构中位数；金融行业不套用普通行业PE估值。',
    }


def load_sector_attention(conn, cutoff, report_date, cfg):
    """Report attention is a bounded source statistic, never capital-flow heat."""
    rows = conn.execute('''WITH eligible AS MATERIALIZED (
        SELECT r.document_id,r.stock_code,r.stock_name,r.industry_name,r.institution,
          r.report_date,greatest(r.available_at,r.created_at,r.updated_at,r.identity_known_at,
            d.created_at,d.updated_at) available_at
        FROM research.reports_resolved r LEFT JOIN corpus.documents d ON d.id=r.document_id
        WHERE r.report_date BETWEEN %(day)s::date-%(days)s AND %(day)s
          AND greatest(r.available_at,r.created_at,r.updated_at,r.identity_known_at,
            d.created_at,d.updated_at)<=%(cutoff)s AND nullif(r.industry_name,'') IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM corpus.document_quarantine q
              WHERE q.document_id=r.document_id AND q.released_at IS NULL)
      ) SELECT industry_name industry,count(DISTINCT document_id) reports,
          count(DISTINCT institution) institutions,count(DISTINCT stock_code) stocks,
          max(report_date) latest_report_date,max(available_at) available_at,
          (array_agg(DISTINCT stock_code) FILTER (WHERE stock_code IS NOT NULL)) stock_codes
        FROM eligible GROUP BY industry_name
        ORDER BY count(DISTINCT institution) DESC,count(DISTINCT document_id) DESC,industry_name
        LIMIT %(limit)s''', {'cutoff':cutoff,'day':report_date, 'days':cfg['report_days'],
                             'limit':cfg['hot_sector_limit']}).fetchall()
    return [{**r, 'window_days':cfg['report_days'], 'meaning':'已收录研报的行业关注度，不是资金流入、涨幅或全网热度'} for r in rows]


def report_watchlist(valuation, pool, sector_attention, cfg):
    """Choose cross-industry editorial subjects without changing pool membership."""
    pool_by_code = {r['stock_code']:r for r in pool if r.get('state') != 'removed'}
    rows, seen, industries = [], set(), Counter()
    # Reserve room for both value discovery and persisted financially screened members.
    for row in valuation[:max(1,cfg['watchlist_limit']//2)] + diversify(
            list(pool_by_code.values()), 'industry', cfg['watchlist_limit'], cfg['max_per_industry']) + valuation:
        if row['stock_code'] in seen:
            continue
        sector=row.get('industry') or '未分类'
        if industries[sector]>=cfg['max_per_industry']:
            continue
        seen.add(row['stock_code'])
        industries[sector]+=1
        member = pool_by_code.get(row['stock_code'])
        attention = [r['industry'] for r in sector_attention if row.get('industry') == r['industry']]
        rows.append({'stock_code':row['stock_code'], 'name':row.get('name'),
            'industry':row.get('industry'), 'decision':'研究选题', 'rank':len(rows)+1,
            'reason':{'selection':'估值筛选' if 'forward_pe' in row else '长期候选池',
                      'hot_sectors':attention, 'pool_state':member.get('state') if member else None,
                      'pool_change':member.get('change') if member else None},
            'research':{'stock_name':row.get('name'), 'actual_financials':member.get('financial_evidence',[]) if member else [],
                        'thesis':member.get('thesis',[]) if member else [],
                        'risks':member.get('financial_risks',[]) if member else [],
                        'next_checks':member.get('next_checks',[]) if member else [],
                        'valuation':row if 'forward_pe' in row else None}})
        if len(rows) >= cfg['watchlist_limit']:
            break
    return rows


def enrich_financials(conn, cutoff, market_date, watchlist, valuation):
    """Reuse the dated financial snapshot without importing its AI watchlist."""
    from scripts.analytics.research_snapshot import load_snapshot, compact_research
    codes=sorted({r['stock_code'] for r in watchlist+valuation})
    if not codes:
        return
    snapshot=compact_research(load_snapshot(conn,cutoff,codes=codes,as_of=market_date,limit=32))
    rows={r['stock_code']:r for r in snapshot.get('rows',[])}
    for item in watchlist:
        research=rows.get(item['stock_code'])
        if research:
            item['research'].update({k:research[k] for k in
                ('actual_financials','financial_ratios','market','as_of_date') if k in research})
    for item in valuation:
        research=rows.get(item['stock_code'])
        if research:
            item['reported_financials']=research.get('actual_financials',[])
            item['financial_ratios']=research.get('financial_ratios',[])
            item['financial_snapshot_date']=research.get('as_of_date')


def selection_receipt(watchlist, documents, reports, valuation, sectors):
    return {'selection_scope':'all_a_share', 'industry_keyword_filter':None,
        'selected_industries':sorted({r['industry'] for r in watchlist if r.get('industry')}),
        'document_sources':dict(Counter(r['source_name'] for r in documents)),
        'report_industries':dict(Counter(r['industry_name'] or '未分类' for r in reports)),
        'valuation_stocks':len(valuation), 'hot_sectors':len(sectors),
        'watchlist_stocks':len(watchlist), 'watchlist_meaning':'本期研究选题，不改变长期候选池状态或持仓'}
