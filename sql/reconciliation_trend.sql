-- How a source's reconciliation has behaved over its recent runs.
--
-- A single failing check is an incident; the same check failing for six runs is
-- a different problem with a different owner. Persisting results per check per
-- run is what makes that distinction visible, and this query is why it was worth
-- persisting them.
--
-- Parameters: :source_id, :limit

WITH recent_runs AS (
    SELECT r.run_id, r.started_at, r.source_id
    FROM meta.pipeline_runs r
    WHERE r.source_id = :source_id
      AND r.status = 'SUCCEEDED'
    ORDER BY r.started_at DESC
    LIMIT :limit
)
SELECT
    rr.check_name,
    rr.dimension,
    rr.check_kind,
    count(*)                                              AS runs_observed,
    count(*) FILTER (WHERE rr.status = 'PASS')            AS passed,
    count(*) FILTER (WHERE rr.status = 'WARN')            AS warned,
    count(*) FILTER (WHERE rr.status = 'FAIL')            AS failed,
    round(avg(rr.delta_pct), 4)                           AS avg_delta_pct,
    max(rr.delta_pct)                                     AS worst_delta_pct,
    -- Most recent verdict first, so a check that has just started failing is
    -- distinguishable from one that has always failed.
    (array_agg(rr.status ORDER BY run.started_at DESC))[1:10] AS recent_verdicts
FROM dq.reconciliation_results rr
JOIN recent_runs run ON run.run_id = rr.run_id
GROUP BY rr.check_name, rr.dimension, rr.check_kind
ORDER BY failed DESC, warned DESC, rr.check_name;
