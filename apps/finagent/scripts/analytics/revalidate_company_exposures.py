"""Recheck stored company/concept attribution; preserve exact previous rows."""
import argparse,json
from collections import Counter
from pathlib import Path
from common import connect,jsonb
from company_evidence_scope import scope_evidence, VERSION

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--audit-dir',type=Path,required=True);ap.add_argument('--apply',action='store_true')
    ap.add_argument('--force',action='store_true')
    ap.add_argument('--original-audit',type=Path,help='Original pre-repair rows, used to retain issuer context when an older repair kept only a clause.')
    args=ap.parse_args()
    original={str(r['exposure_id']):r['evidence'] for r in json.loads(args.original_audit.read_text())} if args.original_audit else {}
    args.audit_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    with connect(autocommit=False) as c:
        rows=c.execute("""SELECT x.*,k.name AS concept_name,k.aliases AS concept_aliases,
                r.stock_code AS document_stock_code
            FROM kg.company_concept_exposures x JOIN kg.concepts k ON k.concept_id=x.concept_id
            LEFT JOIN research.reports_resolved r ON r.document_id=x.evidence_document_id
            WHERE x.status IN ('active','candidate') AND x.review_status<>'approved'
              AND (%s OR coalesce(x.metadata->'attribution_check'->>'version','')<>%s)""",(args.force,VERSION)).fetchall()
        previous=args.audit_dir/'exposures.before.json'
        if not previous.exists():previous.write_text(json.dumps(rows,ensure_ascii=False,default=str,indent=2))
        temporal=c.execute("SELECT * FROM kg.relation_versions WHERE source_system='kg.company_concept_exposures'").fetchall()
        previous=args.audit_dir/'temporal-exposures.before.json'
        if not previous.exists():previous.write_text(json.dumps(temporal,ensure_ascii=False,default=str,indent=2))
        results=[];counts=Counter()
        for r in rows:
            context=original.get(str(r['exposure_id'])) or r['metadata'].get('attribution_original_evidence') or r['evidence']
            check=scope_evidence(context,r['stock_code'],r['stock_name'],[r['concept_name'],*(r['concept_aliases'] or [])],r['document_stock_code'])
            status='active' if check['supported'] else 'rejected' if check['reason'] in ('different_issuer_clause','different_issuer_context','negated_concept') else 'candidate'
            counts[status]+=1;counts[check['reason']]+=1
            results.append({'exposure_id':str(r['exposure_id']),'stock_code':r['stock_code'],'concept':r['concept_name'],'status':status,'check':check})
            if args.apply:
                c.execute("""UPDATE kg.company_concept_exposures SET status=%s,review_status=%s,
                      evidence=coalesce(%s,evidence),evidence_span=coalesce(%s,evidence_span),
                      metadata=metadata || %s,updated_at=now() WHERE exposure_id=%s""",
                      (status,'classified' if status=='active' else 'rejected' if status=='rejected' else 'unreviewed',check['span'],check['span'],jsonb({'attribution_check':check,'attribution_original_evidence':context}),r['exposure_id']))
        if args.apply:
            c.execute("""UPDATE kg.relation_versions v SET status=x.status,
                review_status=CASE WHEN x.status='rejected' THEN 'rejected' ELSE 'unreviewed' END,
                updated_at=now()
                FROM kg.company_concept_exposures x
                WHERE v.source_system='kg.company_concept_exposures'
                  AND v.attributes->>'exposure_id'=x.exposure_id::text
                  AND x.metadata->'attribution_check'->>'version'=%s""",(VERSION,))
            c.commit()
        else:c.rollback()
    (args.audit_dir/'attribution-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    (args.audit_dir/'attribution-summary.json').write_text(json.dumps({'applied':args.apply,'selected':len(rows),'counts':dict(counts)},ensure_ascii=False,indent=2))
    print(json.dumps({'applied':args.apply,'selected':len(rows),'counts':dict(counts)},ensure_ascii=False))

if __name__=='__main__':main()
