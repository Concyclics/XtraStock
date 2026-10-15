"""Conservative extraction of consolidated statements and comparative columns.

Only explicitly headed statements with an explicit monetary unit are accepted.
Comparatives retain their own reporting period and the publication date of the
document that disclosed them. A dash is not silently interpreted as zero.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import re

DECIMAL = re.compile(r'(?<![\d.,])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+')
UNITS = {'元': Decimal(1), '千元': Decimal(1000), '万元': Decimal(10000), '亿元': Decimal(100000000)}
BALANCE = {
    'cash_and_bank': ('货币资金',), 'trading_financial_assets': ('交易性金融资产',),
    'accounts_receivable': ('应收账款',), 'inventory': ('存货',),
    'current_assets': ('流动资产合计',), 'noncurrent_assets': ('非流动资产合计',),
    'total_assets': ('资产总计','总资产'), 'fixed_assets': ('固定资产',),
    'construction_in_progress': ('在建工程',), 'intangible_assets': ('无形资产',),
    'short_term_borrowings': ('短期借款',), 'long_term_borrowings': ('长期借款',),
    'accounts_payable': ('应付账款',), 'contract_liabilities': ('合同负债',),
    'current_liabilities': ('流动负债合计',), 'noncurrent_liabilities': ('非流动负债合计',),
    'total_liabilities': ('负债合计',),
    'equity_parent': ('归属于母公司所有者权益','归属于母公司所有者权','归属于上市公司股东的净资产'),
    'minority_equity': ('少数股东权益',), 'total_equity': ('所有者权益',),
    'liabilities_and_equity': ('负债和所有者权益',),
}
INCOME = {
    'revenue': ('营业收入',), 'total_operating_revenue': ('营业总收入',), 'cost_of_revenue': ('营业成本',),
    'total_operating_cost': ('营业总成本',), 'selling_expenses': ('销售费用',),
    'administrative_expenses': ('管理费用',), 'research_development_expenses': ('研发费用',),
    'finance_expenses': ('财务费用',), 'operating_profit': ('营业利润',),
    'profit_before_tax': ('利润总额',), 'income_tax_expense': ('所得税费用',),
    'net_profit_total': ('净利润',), 'net_profit_parent': ('归属于母公司股东的净利润','归属于母公司所有者的净利','归属于上市公司股东的净利润'),
    'net_profit_minority': ('少数股东损益',), 'eps_basic': ('基本每股收益',),
    'eps_diluted': ('稀释每股收益',),
}
CASHFLOW = {
    'operating_cash_inflow': ('经营活动现金流入小计',),
    'operating_cash_outflow': ('经营活动现金流出小计',),
    'operating_cash_flow': ('经营活动产生的现金流量',),
    'investing_cash_flow': ('投资活动产生的现金流量',),
    'financing_cash_flow': ('筹资活动产生的现金流量',),
    'capex_cash_paid': ('购建固定资产、无形资产和',),
    'cash_fx_effect': ('汇率变动对现金及现金',),
    'cash_net_change': ('现金及现金等价物净增加额','现金及现金等价物净增'),
    'cash_beginning': ('期初现金及现金等价物余','期初/年初现金及现金'),
    'cash_ending': ('期末现金及现金等价物余额','期末/年末现金及现金'),
}
SUMMARY = {
    'revenue': ('营业收入',), 'net_profit_parent': ('归属于上市公司股东的净利润',),
    'profit_before_tax': ('利润总额',),
    'deducted_net_profit': ('归属于上市公司股东的扣除非','扣除非经常性损益后的净利润'),
    'operating_cash_flow': ('经营活动产生的现金流量净额',),
    'equity_parent': ('归属于上市公司股东的净资产',), 'total_assets': ('总资产',),
    'eps_basic': ('基本每股收益',), 'eps_diluted': ('稀释每股收益',),
    'eps_deducted': ('扣除非经常性损益后的基本每股收益',),
    'roe_weighted': ('加权平均净资产收益率',),
    'roe_deducted': ('扣除非经常性损益后的加权平均净资产收益率',),
    'rd_revenue_ratio': ('研发投入占营业收入的比例',),
}
NAMES = {code: labels[0] for group in (BALANCE,INCOME,CASHFLOW,SUMMARY) for code,labels in group.items()}


def compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def label_text(text: str) -> str:
    text = compact(text)
    text = re.sub(r'[一二三四五六七八九十]+、\d+', '', text)
    text = re.sub(r'^(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)]|\d+[.．])', '', text)
    return re.sub(r'^(?:其中|加|减)[:：]', '', text)


def extract_rows(lines: list[str], definitions: dict) -> list[dict]:
    pending = ''
    out = []
    aliases = sorted([(alias,code) for code,labels in definitions.items() for alias in labels],
                     key=lambda pair: len(pair[0]), reverse=True)
    for line in lines:
        matches = list(DECIMAL.finditer(line))
        if len(matches) < 2:
            if line.strip() and not re.search(r'有限公司|\d+\s*/\s*\d+|单位[:：]|公司负责人', line):
                pending = line
            continue
        prefix = label_text(line[:matches[0].start()])
        candidates = [prefix] if prefix else [label_text(pending)]
        matched = next(((code,label) for label,code in aliases
                        if any(candidate.startswith(label) for candidate in candidates)), None)
        if matched:
            code,_ = matched
            out.append({'metric_code':code,'values':[Decimal(m.group().replace(',','').replace('−','-')) for m in matches],
                        'source_text':(pending+'\n'+line).strip()[-1800:]})
        pending = ''
    return out


def parse_statements(text: str, report_end: dt.date) -> tuple[list[dict], list[dict]]:
    lines = text.replace('\r','').splitlines()
    sections=[]
    for title,kind,definitions in [('合并资产负债表','balance',BALANCE),('合并利润表','income',INCOME),('合并现金流量表','cashflow',CASHFLOW)]:
        starts=[i for i,line in enumerate(lines) if compact(line)==title]
        for start in starts[:1]:
            end=next((i for i in range(start+1,len(lines)) if compact(lines[i]).startswith('公司负责人：')
                      or compact(lines[i]) in ('母公司资产负债表','母公司利润表','母公司现金流量表')),len(lines))
            sections.append((kind,definitions,lines[start:end]))
    for i,line in enumerate(lines):
        if re.match(r'^(?:\(一\)|（一）)主要会计数据$',compact(line)) or re.match(r'^2\.2主要财务数据$',compact(line)):
            end=next((j for j in range(i+1,len(lines)) if compact(lines[j]).startswith(('2.3','公司主要会计数据和财务指标的说明'))),min(len(lines),i+100))
            sections.append(('summary',SUMMARY,lines[i:end]));break
    found={};checks=[]
    for kind,definitions,section in sections:
        header=compact('\n'.join(section[:12]))
        unit_match=re.search(r'单位[:：](亿元|万元|千元|元)',header)
        if not unit_match:
            raise ValueError(f'{kind}: missing explicit monetary unit')
        factor=UNITS[unit_match.group(1)]
        if kind!='summary' and (str(report_end.year) not in header or str(report_end.year-1) not in header):
            raise ValueError(f'{kind}: reporting/comparative years do not match the expected period')
        for row in extract_rows(section,definitions):
            code=row['metric_code']; point=code in BALANCE
            comparative=dt.date(report_end.year-1,12,31) if point else report_end.replace(year=report_end.year-1)
            unit='CNY/share' if code.startswith('eps_') else '%' if code in ('roe_weighted','roe_deducted','rd_revenue_ratio') else 'CNY'
            multiplier=Decimal(1) if unit!='CNY' else factor
            for column,date in enumerate((report_end,comparative)):
                value=row['values'][column]*multiplier
                key=(code,date)
                if key in found and found[key]['value']!=value:
                    raise ValueError(f'Conflicting statement/summary values for {code} at {date}')
                record={'metric_code':code,'metric_name':NAMES[code],'value':value,'yoy':None,'unit':unit,
                        'period_end':date,'period_start':None if point else dt.date(date.year,1,1),
                        'period_type':'point_in_time' if point else 'annual' if date.month==12 else 'year_to_date',
                        'source_text':row['source_text'],'source_unit':unit_match.group(1),
                        'statement_section':kind,'comparative':column==1}
                if key not in found or kind!='summary':
                    found[key]=record
            # Only an explicit numeric third column in the summary supplies a
            # growth/change rate. Comparative values never acquire current YoY.
            if kind=='summary' and len(row['values'])==3 and code not in ('roe_weighted','roe_deducted','rd_revenue_ratio'):
                found[(code,report_end)]['yoy']=row['values'][2]
    return list(found.values()), validate_identities(found)


def validate_identities(found: dict) -> list[dict]:
    checks=[]
    equations=[('balance','total_assets',('total_liabilities','total_equity')),
               ('assets','total_assets',('current_assets','noncurrent_assets')),
               ('liabilities','total_liabilities',('current_liabilities','noncurrent_liabilities')),
               ('equity','total_equity',('equity_parent','minority_equity')),
               ('net_profit','net_profit_total',('net_profit_parent','net_profit_minority')),
               ('cash_change','cash_net_change',('operating_cash_flow','investing_cash_flow','financing_cash_flow','cash_fx_effect')),
               ('cash_balance','cash_ending',('cash_beginning','cash_net_change'))]
    for date in sorted({key[1] for key in found}):
        for name,left,right in equations:
            if all((code,date) in found for code in (left,*right)):
                error=found[(left,date)]['value']-sum((found[(code,date)]['value'] for code in right),Decimal(0))
                if abs(error)>Decimal('0.02'):
                    raise ValueError(f'{name} identity failed at {date}: difference={error}')
                checks.append({'check':name,'period_end':date.isoformat(),'difference':str(error),'passed':True})
    return checks


def parse_annual_prospectus(text: str) -> tuple[list[dict], list[dict]]:
    """Read the explicitly headed three-year consolidated IPO statements.

    Annual column years are read from the source header. Dashes preserve column
    positions and remain missing, never zero. Stop at the next numbered section
    so that parent-company or narrative values cannot bleed into the statements.
    """
    lines=text.replace('\r','').splitlines()
    found={}
    token_re=re.compile(DECIMAL.pattern + r'|(?<!\S)[—-](?!\S)')
    for title,kind,definitions in [('合并资产负债表','balance',BALANCE),
                                  ('合并利润表','income',INCOME),
                                  ('合并现金流量表','cashflow',CASHFLOW)]:
        starts=[i for i,line in enumerate(lines) if label_text(line)==title]
        if len(starts)!=1:
            raise ValueError(f'{kind}: expected one explicit consolidated statement')
        start=starts[0]
        end=next((i for i in range(start+1,len(lines))
                  if re.match(r'^(?:[（(][一二三四五六七八九十]+[）)]|[一二三四五六七八九十]+、)'
                              r'(?:合并|母公司|会计师事务所|审计意见)',compact(lines[i]))),len(lines))
        # Adjacent statement headings take precedence over internal item labels.
        end=min(end,next((i for i in range(start+1,len(lines)) if re.match(
            r'^[（(][一二三四五六七八九十]+[）)]合并(?:资产负债|利润|现金流量)表$',compact(lines[i]))),len(lines)))
        section=lines[start:end]
        header='\n'.join(section[:8])
        unit=re.search(r'单位\s*[:：]\s*(亿元|万元|千元|元)',header)
        years=list(dict.fromkeys(int(y) for y in re.findall(r'(20\d{2})\s*年',header)))
        if not unit or len(years)!=3 or years!=list(range(years[0],years[0]-3,-1)):
            raise ValueError(f'{kind}: missing unit or unrecognized annual columns')
        if kind!='balance' and not re.search(r'20\d{2}\s*年度',header):
            raise ValueError(f'{kind}: not an annual statement')
        aliases=sorted([(alias,code) for code,labels in definitions.items() for alias in labels],
                       key=lambda pair:len(pair[0]),reverse=True)
        pending=''
        for line in section:
            tokens=list(token_re.finditer(line))
            if len(tokens)!=3:
                if line.strip(): pending=line
                continue
            prefix=label_text(line[:tokens[0].start()]) or label_text(pending)
            code=next((code for alias,code in aliases if prefix.startswith(alias)),None)
            if code:
                for column,(year,token) in enumerate(zip(years,tokens)):
                    if token.group() in ('-','—'):continue
                    date=dt.date(year,12,31)
                    unit_name='CNY/share' if code.startswith('eps_') else 'CNY'
                    value=Decimal(token.group().replace(',','').replace('−','-'))
                    if unit_name=='CNY':value*=UNITS[unit.group(1)]
                    record={'metric_code':code,'metric_name':NAMES[code],'value':value,'yoy':None,
                            'unit':unit_name,'period_end':date,
                            'period_start':None if kind=='balance' else dt.date(year,1,1),
                            'period_type':'point_in_time' if kind=='balance' else 'annual',
                            'source_text':(pending+'\n'+line).strip()[-1800:],
                            'source_unit':unit.group(1),'statement_section':kind,'comparative':column>0}
                    key=(code,date)
                    if key in found and found[key]['value']!=value:
                        raise ValueError(f'Conflicting annual values for {key}')
                    found[key]=record
            pending=''
    checks=validate_identities(found)
    if not all(any(c['check']==name for c in checks) for name in ('balance','net_profit','cash_change','cash_balance')):
        raise ValueError('Incomplete annual statement identities')
    return list(found.values()),checks
