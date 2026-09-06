-- One row per run: what it processed, how it reconciled, and how long it took.
--
-- Deliberately a single query with lateral joins rather than several round
-- trips. The stage timings come from meta.run_stages pivoted into columns, so
-- "which stage is getting slower?" is answerable without a second query.
--
-- Parameters:
--   :run_id     a single run, or NULL for a listing
--   :source_id  filter a listing by source, or NULL for all sources
--   :limit      maximum rows for a listing

SELECT
    r.run_id,
    r.source_id,
    s.display_name          AS source_name,
    cv.version              AS contract_version,
    r.trigger,
    r.status,
    r.is_replay,
    r.reconciliation_status,
    r.started_at,
    r.finished_at,
    round(extract(epoch FROM (r.finished_at - r.started_at))::numeric, 3) AS duration_seconds,
    r.records_received,
    r.records_valid,
    r.records_quarantined,
    CASE WHEN r.records_received = 0 THEN 0
         ELSE round(r.records_quarantined * 100.0 / r.records_received, 2)
    END                     AS quarantine_rate_pct,
    r.rows_inserted,
    r.rows_updated,
    r.rows_unchanged,
    r.rows_superseded,
    stages.timings,
    r.error_type,
    r.error_message
FROM meta.pipeline_runs r
JOIN meta.sources s            ON s.source_id = r.source_id
JOIN meta.contract_versions cv ON cv.contract_version_id = r.contract_version_id
LEFT JOIN LATERAL (
    SELECT jsonb_object_agg(
               st.stage,
               jsonb_build_object(
                   'status', st.status,
                   'ms', round(extract(epoch FROM (st.finished_at - st.started_at)) * 1000)
               )
           ) AS timings
    FROM meta.run_stages st
    WHERE st.run_id = r.run_id
) stages ON TRUE
-- CAST rather than the `::` shorthand. Two reasons, both non-obvious:
--   1. Postgres cannot infer a parameter's type from `$1 IS NULL` alone.
--   2. SQLAlchemy's text() parser ignores a placeholder immediately followed by
--      a colon, so the shorthand would leave it unbound.
-- A NULL value means "no filter".
WHERE (CAST(:run_id AS uuid) IS NULL OR r.run_id = CAST(:run_id AS uuid))
  AND (CAST(:source_id AS text) IS NULL OR r.source_id = CAST(:source_id AS text))
ORDER BY r.started_at DESC
LIMIT :limit;
