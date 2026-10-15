"""Append semantic corporate-action revisions; never overwrite historical facts."""
from __future__ import annotations
import re
from psycopg.types.json import Jsonb

FIELDS=('asset_id','action_type','announced_at','record_date','ex_date','pay_date',
        'split_ratio','stock_dividend_ratio','cash_dividend','currency','source_url',
        'source_document_id','source_external_id','quality_status')
META=('parser','differential_distribution','effective_cash_ratio_parsed','effective_share_change_parsed',
      'declared_cash_dividend','declared_split_ratio','declared_stock_dividend_ratio','ex_date_recovered_from_pay_date')
PARSER_VERSION='cninfo_equity_distribution:v5'


def save_distribution(conn,filing,action,path):
    external_id=str(filing['source_external_id'])
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('cninfo:distribution:'+external_id,))
    old=conn.execute('''SELECT * FROM market.corporate_actions WHERE source_name='cninfo' AND source_external_id=%s
       ORDER BY revision_no DESC LIMIT 1''',(external_id,)).fetchone()
    if old and old['quality_status']=='rejected' and old['metadata'].get('superseded_by_external_id'):
        return {'changed':False,'revision_no':old['revision_no'],'action_id':str(old['action_id']),
                'reason':'officially superseded plan; do not reactivate by reparsing'}
    if re.search(r'发生调整|更正|修订|更新后',filing.get('title') or '') and not (old and old['metadata'].get('supersession_reviewed')):
        raise ValueError('implementation amendment requires an explicit supersession review before becoming an active corporate action')
    quality=action['quality_status']
    # Parsing the same PDF again is not independent evidence for upgrading a
    # source-quality decision made by a prior repair or curator.
    if old and old['quality_status']=='unverified' and quality=='verified':quality='unverified'
    record={**{k:action[k] for k in ('action_type','record_date','ex_date','pay_date','split_ratio','stock_dividend_ratio','cash_dividend')},
       'asset_id':filing['asset_id'],'announced_at':filing['announced_at'],'currency':'CNY','source_url':filing['source_url'],
       'source_document_id':filing['document_id'],'source_external_id':external_id,'quality_status':quality}
    metadata={**(old['metadata'] if old else {}),'parser':PARSER_VERSION,'official_source':True,
       'pdf_path':str(path),'unit':'per_share',**{k:action[k] for k in META if k!='parser'}}
    # str normalizes UUID adapters; exact floats/dates retain parser precision.
    equal=lambda a,b: a==b or (a is not None and b is not None and str(a)==str(b))
    if old and all(equal(old[k],record[k]) for k in FIELDS) and all(equal(old['metadata'].get(k),metadata[k]) for k in META):
        return {'changed':False,'revision_no':old['revision_no'],'action_id':str(old['action_id'])}
    revision=(old['revision_no'] if old else 0)+1
    metadata.update(corrected_from_action_id=str(old['action_id']) if old else None,
                    source_document_available_at=str(filing['available_at']),
                    availability_policy='observed at parsing; source publication is a separate timestamp')
    columns=FIELDS+('source_name','revision_no','metadata')
    values=[record[k] for k in FIELDS]+['cninfo',revision,Jsonb(metadata)]
    inserted=conn.execute('INSERT INTO market.corporate_actions ('+','.join(columns)+',available_at,updated_at) VALUES ('+
       ','.join(['%s']*len(columns))+',clock_timestamp(),clock_timestamp()) RETURNING action_id',values).fetchone()
    return {'changed':True,'revision_no':revision,'action_id':str(inserted['action_id'])}
