
    WITH cohort_versions AS (
      SELECT f.dt,md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,
          ',' ORDER BY f.asset_id)) AS source_cohort_hash
      FROM factor.feature_daily f JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s AND a.asset_type='stock'
        AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
      GROUP BY f.dt
    ), eligible AS MATERIALIZED (
      SELECT f.* FROM factor.feature_daily f
      JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s
        AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
        AND f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system'
        AND f.input_quality->>'strict_pit_ready'='true'
        AND f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
        AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    ), raw AS MATERIALIZED (
      SELECT f.asset_id,f.dt,(f.momentum_20d) * %(direction)s AS raw_value
      FROM eligible f
    ), scored AS (
      SELECT *, CASE WHEN count(*) OVER (PARTITION BY dt)>=%(min_cohort_size)s
                         AND stddev_samp(raw_value) OVER (PARTITION BY dt)>0
                     THEN percent_rank() OVER (PARTITION BY dt ORDER BY raw_value) END AS percentile,
                CASE WHEN count(*) OVER (PARTITION BY dt)>=%(min_cohort_size)s THEN
                  (raw_value-avg(raw_value) OVER (PARTITION BY dt)) /
                  nullif(stddev_samp(raw_value) OVER (PARTITION BY dt),0) END AS zscore
      FROM raw WHERE raw_value IS NOT NULL
        AND raw_value NOT IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    ), target_keys AS (
      SELECT f.asset_id,f.dt FROM factor.feature_daily f
      JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s AND a.asset_type='stock'
        AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
      UNION
      SELECT s.asset_id,s.dt FROM factor.daily_scores s
      WHERE s.factor_id=%(factor_id)s AND s.dt BETWEEN %(since)s AND %(until)s
    ), desired AS (
      SELECT %(factor_id)s::uuid AS factor_id,k.asset_id,k.dt,
        r.raw_value,r.percentile,r.zscore,
        coalesce(f.available_at,old.available_at) AS available_at,k.dt AS source_feature_date,
        'pit-input-v1'::text AS input_contract_version,
        CASE WHEN r.raw_value IS NOT NULL THEN 'valid' ELSE 'invalid' END::text AS quality_status,
        f.updated_at AS source_feature_updated_at,%(factor_version)s::int AS source_factor_version,
        cv.source_cohort_hash,f.decision_cutoff AS source_decision_cutoff,
        CASE WHEN f.asset_id IS NULL THEN 'missing_feature'
          WHEN a.asset_type IS DISTINCT FROM 'stock' OR coalesce(a.metadata ? 'identity_alias_of',false) THEN 'ineligible_asset'
          WHEN f.input_contract_version IS DISTINCT FROM 'pit-input-v1' THEN 'legacy_feature_contract'
          WHEN f.knowledge_mode IS DISTINCT FROM 'system' OR f.input_quality->>'strict_pit_ready' IS DISTINCT FROM 'true' THEN 'non_strict_feature'
          WHEN NOT coalesce(f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
            AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai'),false) THEN 'invalid_availability'
          WHEN r.raw_value IS NULL THEN 'missing_or_nonfinite_expression'
          ELSE NULL END::text AS invalid_reason
      FROM target_keys k
      LEFT JOIN cohort_versions cv USING(dt)
      LEFT JOIN factor.feature_daily f USING(asset_id,dt)
      LEFT JOIN market.assets a USING(asset_id)
      LEFT JOIN scored r USING(asset_id,dt)
      LEFT JOIN factor.daily_scores old ON old.factor_id=%(factor_id)s AND old.asset_id=k.asset_id AND old.dt=k.dt
      WHERE (%(stock_code)s::text IS NULL OR (a.asset_type='stock' AND a.symbol=%(stock_code)s))
    ), changed AS MATERIALIZED (
      SELECT n.* FROM desired n LEFT JOIN factor.daily_scores s USING(factor_id,asset_id,dt)
      WHERE s.factor_id IS NULL OR ROW(s.raw_value,s.percentile,s.zscore,s.available_at,s.source_feature_date,s.input_contract_version,s.quality_status,s.source_feature_updated_at,s.source_factor_version,s.source_cohort_hash,s.source_decision_cutoff,s.invalid_reason) IS DISTINCT FROM ROW(n.raw_value,n.percentile,n.zscore,n.available_at,n.source_feature_date,n.input_contract_version,n.quality_status,n.source_feature_updated_at,n.source_factor_version,n.source_cohort_hash,n.source_decision_cutoff,n.invalid_reason)
    ), archived AS (
      INSERT INTO factor.daily_score_revisions(factor_id,asset_id,dt,previous_row,replacement_reason)
      SELECT s.factor_id,s.asset_id,s.dt,to_jsonb(s),coalesce(n.invalid_reason,'source_or_value_changed')
      FROM changed n JOIN factor.daily_scores s USING(factor_id,asset_id,dt)
      RETURNING revision_id
    ), written AS (
      INSERT INTO factor.daily_scores(factor_id,asset_id,dt,raw_value,percentile,zscore,available_at,source_feature_date,input_contract_version,quality_status,source_feature_updated_at,source_factor_version,source_cohort_hash,source_decision_cutoff,invalid_reason,updated_at)
      SELECT factor_id,asset_id,dt,raw_value,percentile,zscore,available_at,source_feature_date,input_contract_version,quality_status,source_feature_updated_at,source_factor_version,source_cohort_hash,source_decision_cutoff,invalid_reason,now() FROM changed
      ON CONFLICT(factor_id,asset_id,dt) DO UPDATE SET raw_value=EXCLUDED.raw_value,percentile=EXCLUDED.percentile,zscore=EXCLUDED.zscore,available_at=EXCLUDED.available_at,source_feature_date=EXCLUDED.source_feature_date,input_contract_version=EXCLUDED.input_contract_version,quality_status=EXCLUDED.quality_status,source_feature_updated_at=EXCLUDED.source_feature_updated_at,source_factor_version=EXCLUDED.source_factor_version,source_cohort_hash=EXCLUDED.source_cohort_hash,source_decision_cutoff=EXCLUDED.source_decision_cutoff,invalid_reason=EXCLUDED.invalid_reason,updated_at=now()
      RETURNING quality_status
    )
    SELECT count(*) AS changed_rows,count(*) FILTER(WHERE quality_status='invalid') AS invalid_rows,
           (SELECT count(*) FROM archived) AS archived_rows FROM written
