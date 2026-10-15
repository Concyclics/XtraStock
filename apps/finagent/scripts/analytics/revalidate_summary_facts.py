"""Recheck stored scoped-summary facts against their retained official PDF text.

Only the summary parser's own facts are touched. Missing/ambiguous fields leave
the verified dataset; exact previous rows are retained before numeric changes.
"""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from common import connect,jsonb
from cninfo_summary_parser import parse_summary,issuer_identity,VERSION


def lock_unchanged(conn, fact):
    # Confirming an unchanged row must remain read-only: FOR UPDATE on every
    # confirmed fact needlessly generates WAL for the entire historical set.
    current=conn.execute('SELECT * FROM company.financial_facts WHERE financial_fact_id=%s FOR UPDATE',
                         (fact['financial_fact_id'],)).fetchone()
    if current != fact:
        raise RuntimeError(f"Fact {fact['financial_fact_id']} changed concurrently; resume revalidation")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--audit-dir',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--filing-id-file',type=Path,help='JSON array for a bounded follow-up check.')
    args=parser.parse_args()
    selected=json.loads(args.filing_id_file.read_text()) if args.filing_id_file else None
    args.audit_dir.mkdir(mode=0o700,parents=True,exist_ok=args.resume)
    counts=Counter()
    seen=set()
    if args.resume and (args.audit_dir/'outcomes.jsonl').exists():
        for line in (args.audit_dir/'outcomes.jsonl').read_text().splitlines():
            row=json.loads(line);seen.add(row['filing_id']);counts['documents']+=1
            counts.update({k:v for k,v in row.items() if k in ('confirmed','corrected','unverified')})
    with (args.audit_dir/'changed-facts.before.jsonl').open('a' if args.resume else 'w') as backup, (args.audit_dir/'outcomes.jsonl').open('a' if args.resume else 'w') as outcomes:
        with connect(autocommit=False) as reader,connect(autocommit=False) as writer:
            with reader.cursor(name='summary_facts_recheck') as cursor:
                cursor.itersize=30
                cursor.execute("""WITH eligible AS MATERIALIZED (
                  SELECT DISTINCT filing_id FROM company.financial_facts
                  WHERE metadata->>'parser' LIKE 'cninfo_scoped_summary:%%' AND quality_status='verified'
                    AND NOT(filing_id=ANY(%s::uuid[]))
                    AND (%s::uuid[] IS NULL OR filing_id=ANY(%s::uuid[]))
                ) SELECT f.filing_id,f.period_end,f.stock_code,d.id AS document_id,
                  coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,'') AS body
                  FROM eligible e JOIN company.filings f USING(filing_id) JOIN corpus.documents d ON d.id=f.document_id
                  ORDER BY f.filing_id""",(list(seen),selected,selected))
                for doc in cursor:
                    if str(doc['filing_id']) in seen:continue
                    parsed,warnings=parse_summary(doc['body'],doc['period_end'])
                    identity = issuer_identity(doc['body'],doc['stock_code'])
                    by_code={r['metric_code']:r for r in parsed}
                    facts=writer.execute("""SELECT * FROM company.financial_facts
                      WHERE filing_id=%s AND (metadata->>'parser' LIKE 'cninfo_scoped_summary:%%' OR %s)
                        AND quality_status='verified'""",(doc['filing_id'],identity['conflict'])).fetchall()
                    local=Counter()
                    for fact in facts:
                        source=None if identity['conflict'] else by_code.get(fact['metric_code'])
                        metadata=dict(fact['metadata'] or {})
                        if source is None:
                            lock_unchanged(writer,fact)
                            backup.write(json.dumps(dict(fact),default=str,ensure_ascii=False)+'\n');backup.flush()
                            metadata.update(revalidation_required=True,
                              revalidation_reason='issuer_identity_conflict' if identity['conflict'] else 'summary_field_not_recovered',
                              issuer_identity=identity,rechecked_version=VERSION)
                            writer.execute("""UPDATE company.financial_facts SET quality_status='unverified',confidence=least(confidence,0.5),metadata=%s
                              WHERE financial_fact_id=%s""",(jsonb(metadata),fact['financial_fact_id']))
                            local['unverified']+=1
                            continue
                        comparative=metadata.get('comparative') is True
                        value=Decimal(source['prior_value']) if comparative else source['value']
                        yoy=None if comparative else source['yoy']
                        changed=(value!=fact['value'] or yoy!=fact['yoy'] or source['unit']!=fact['unit'])
                        if not changed:
                            local['confirmed']+=1
                            continue
                        if changed:
                            lock_unchanged(writer,fact)
                            backup.write(json.dumps(dict(fact),default=str,ensure_ascii=False)+'\n');backup.flush()
                        metadata.update(parser=VERSION,source_unit=source['source_unit'],revalidation_required=False,
                          rechecked_version=VERSION,comparative_basis=source.get('comparative_basis','reported'),
                          field_checks=source['field_checks'],source_line=source['source_line'],source_page=source['source_page'],parse_warnings=warnings)
                        writer.execute("""UPDATE company.financial_facts SET value=%s,yoy=%s,unit=%s,
                          source_text=%s,metadata=%s WHERE financial_fact_id=%s""",
                          (value,yoy,source['unit'],source['source_text'],jsonb(metadata),fact['financial_fact_id']))
                        local['corrected' if changed else 'confirmed']+=1
                    writer.commit()
                    counts.update(local);counts['documents']+=1
                    outcomes.write(json.dumps({'document_id':str(doc['document_id']),'filing_id':str(doc['filing_id']),
                      'checked_fact_ids':[r['financial_fact_id'] for r in facts],**local})+'\n');outcomes.flush()
                    if counts['documents']%200==0: print(json.dumps(dict(counts)),flush=True)
    result={'parser':VERSION,**counts}
    (args.audit_dir/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
