"""Typed, evidence-bearing earnings forecasts. Never infer a year from a report date.

The Eastmoney list API's ThisYear fields use the response's currentYear, which
can differ from publishDate. Old crawls did not retain that response field.
Text extraction requires an explicit year sequence and per-metric units.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata
from decimal import Decimal

CONTRACT = 'earnings-forecast-v2'
PARSER = 'dated-forecast-text-v1.1'
NUM = r'-?\d+(?:\.\d+)?'
SERIES = rf'{NUM}(?:\s*(?:/|、|,|及|和)\s*{NUM}){{1,3}}'
YEARS = re.compile(r'(?<!\d)((?:20)?\d{2})\s*(?:[-~至—–]\s*((?:20)?\d{2})|((?:\s*[/、,]\s*(?:20)?\d{2}){1,3}))\s*年')
EPS = re.compile(r'(?:EPS|每股收益)(?:\s*\((?:元|元/股)\))?\s*(?:分别)?\s*(?:为|是|约为|约)?\s*('+SERIES+r')\s*(?:元(?:/股)?)',re.I)
PE_SERIES = rf'{NUM}(?:\s*(?:倍|x)?\s*(?:/|、|,|及|和)\s*{NUM}){{1,3}}'
PE = re.compile(r'(?:P\s*/?\s*E|市盈率)(?:分别)?\s*(?:为|是|约为|约)?\s*('+PE_SERIES+r')\s*(?:倍|x)',re.I)
PROFIT = re.compile(r'(?:归母净利润|归母净利|归属于母公司(?:所有者|股东)的?净利润)\s*(?:分别)?\s*(?:为|是|约为|约)?\s*('+SERIES+r')\s*(亿元|百万元|万元|元)')

def explicit_years(match):
    first=int(match.group(1));first=first+2000 if first<100 else first
    if match.group(2):
        last=int(match.group(2));last=last+2000 if last<100 else last
        result=list(range(first,last+1))
    else:
        result=[first]+[int(s)+2000 if int(s)<100 else int(s) for s in re.findall(r'\d+',match.group(3))]
    return result if 2<=len(result)<=4 and result==list(range(first,first+len(result))) else []

def extract_dated_forecasts(text, report_year):
    """Conservative sentence parser; returns rejected ambiguities separately.

    Does not parse estimates from unrelated paragraphs, parenthesized old
    estimates, growth percentages, ranges, multiple companies, or table OCR.
    Values keep the precision in the actual dated evidence.
    """
    normalized=unicodedata.normalize('NFKC',text or '').replace('\u3000',' ')
    accepted={}; rejected=[]
    for paragraph in re.split(r'[\n。；;]',normalized):
        paragraph=re.sub(r'\s+','',paragraph)
        if not re.search(r'预计|预测|预期|我们维持|我们上调|我们下调',paragraph):continue
        matches=list(YEARS.finditer(paragraph))
        for pos,ym in enumerate(matches):
            years=explicit_years(ym)
            if not years or years[0]<report_year-1 or years[-1]>report_year+4:continue
            tail=paragraph[ym.end():matches[pos+1].start() if pos+1<len(matches) else None]
            # Remove old estimates; do not silently read an old parenthesized series.
            tail=re.sub(r'\([^)]*\)','',tail)
            if len(tail)>700:tail=tail[:700]
            # Multiple named companies / segment profits require a richer parser.
            if re.search(r'子公司|收购标的|分部|板块净利',tail):continue
            for field,pattern,unit in [('eps',EPS,'CNY/share'),('pe',PE,'times'),('net_profit',PROFIT,'CNY')]:
                candidates=list(pattern.finditer(tail))
                if len(candidates)!=1:continue
                m=candidates[0]
                values=[Decimal(v) for v in re.findall(NUM,m.group(1))]
                if len(values)!=len(years):
                    rejected.append({'reason':'year_value_count_mismatch','field':field,'evidence':paragraph});continue
                if field=='net_profit':
                    factor={'亿元':Decimal('1e8'),'百万元':Decimal('1e6'),'万元':Decimal('1e4'),'元':Decimal(1)}[m.group(2)]
                    values=[v*factor for v in values]
                for year,value in zip(years,values):
                    k=(year,field)
                    item={'forecast_year':year,'field':field,'value':str(value),'unit':unit,'evidence':paragraph,
                          'parser':PARSER,'statement_scope':'parent' if field=='net_profit' else 'per_share' if field=='eps' else 'valuation'}
                    if k in accepted and accepted[k] and Decimal(accepted[k]['value'])!=value:
                        rejected.append({'reason':'conflicting_explicit_values','year':year,'field':field,'evidence':paragraph});accepted[k]=None
                    elif k not in accepted:accepted[k]=item
    values=[v for v in accepted.values() if v is not None]
    # A report's different forecast years refer to the same observed price.
    # Catch source/OCR typos such as EPS=10.87 instead of .87, without using
    # realized outcomes or silently correcting the author's number.
    by_year={}
    for v in values:by_year.setdefault(v['forecast_year'],{})[v['field']]=Decimal(v['value'])
    prices=[v['eps']*v['pe'] for v in by_year.values() if v.get('eps',0)>0 and v.get('pe',0)>0]
    shares=[v['net_profit']/v['eps'] for v in by_year.values() if v.get('eps',0)>0 and v.get('net_profit',0)>0]
    if (len(prices)>=2 and max(prices)/min(prices)>Decimal('1.35')) or (len(shares)>=2 and max(shares)/min(shares)>Decimal('1.5')):
        rejected.append({'reason':'inconsistent_implied_price_or_shares','evidence':values})
        values=[v for v in values if v['field']=='net_profit']
    return values,rejected

def api_years(row):
    # Only the new crawler's explicit basis is trusted. Legacy 预测当年 was
    # publishDate.year and is known to mislabel 2026 EPS as 2024/2025 EPS.
    if row.get('预测年份口径')!='api.currentYear':return ('','','')
    try:y=int(float(row.get('预测当年') or 0))
    except (TypeError,ValueError):return ('','','')
    return tuple(str(y+i) for i in range(3)) if 2000<=y<=2100 else ('','','')

def evidence_metadata(items, text):
    return {'semantic_contract':CONTRACT,'year_verified':True,'year_basis':'explicit_dated_report_text',
        'eps_unit':'CNY/share','net_profit_unit':'CNY','net_profit_scope':'parent',
        'pe_unit':'times','parser':PARSER,'content_sha256':hashlib.sha256((text or '').encode()).hexdigest(),
        'evidence':items,'availability_mode':'historical_public_reconstruction'}
