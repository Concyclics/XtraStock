"""Bind a concept to the issuer in its own evidence clause, not a nearby story."""
from __future__ import annotations
import re

VERSION = "company_clause:v3.1"
CODES = re.compile(r"(?<!\d)(?:[0368]\d{5}|920\d{3})(?!\d)")


def term_pattern(term):
    term = str(term or '').strip()
    if not term: return None
    escaped = re.escape(term)
    return re.compile(r'(?<![A-Za-z0-9])'+escaped+r'(?![A-Za-z0-9])' if re.fullmatch(r'[A-Za-z0-9.+-]+',term) else escaped,re.I)


def scope_evidence(evidence, stock_code, stock_name, terms, document_stock_code=None):
    evidence = str(evidence or '')
    code = str(stock_code or '')
    name = str(stock_name or '').strip()
    patterns = [(str(t),term_pattern(t)) for t in dict.fromkeys(terms) if t]
    reasons = []
    for sentence in re.finditer(r'[^。；;！？!?\n]+[。；;！？!?]?',evidence):
        span = sentence.group().strip()
        hits = [(term,p.search(span)) for term,p in patterns if p and p.search(span)]
        if not hits: continue
        local_codes = set(CODES.findall(span))
        explicit = code in local_codes or bool(name and name in span)
        if len(local_codes)>1:
            reasons.append('multiple_issuers_in_clause');continue
        preceding_codes = list(CODES.finditer(evidence[:sentence.start()]))
        prior_owner = preceding_codes[-1].group() if preceding_codes else None
        if local_codes and code not in local_codes and not explicit:
            reasons.append('different_issuer_clause'); continue
        if not explicit and prior_owner and prior_owner != code:
            reasons.append('different_issuer_context'); continue
        inherited = (document_stock_code == code or prior_owner == code) and bool(re.search(r'本公司|我司|我们|^(?:公司|其产品|主营|量产|出货)',span))
        if not explicit and not inherited:
            reasons.append('issuer_not_bound'); continue
        positive_hits=[]
        for term,hit in hits:
            before=span[max(0,hit.start()-18):hit.start()]
            after=span[hit.end():hit.end()+22]
            negated = re.search(r'(?:无|非)$',before) or re.search(r'(?:不涉及|尚未涉及|未涉及|没有|不存在|尚未开展|未开展|不从事|尚未布局)[^，。；]{0,8}$',before) or re.match(r'[^，。；]{0,6}(?:尚无|暂无|不存在)(?:相关)?(?:业务|产品|收入)',after)
            if not negated: positive_hits.append(term)
        if not positive_hits:
            reasons.append('negated_concept');continue
        return {'supported':True,'reason':'issuer_clause','span':span,'start':sentence.start(),'end':sentence.end(),
                'matched_terms':positive_hits,'version':VERSION}
    reason = next((x for x in ('negated_concept','different_issuer_clause','different_issuer_context') if x in reasons), reasons[0] if reasons else 'concept_not_in_evidence')
    return {'supported':False,'reason':reason,'span':None,'version':VERSION}
