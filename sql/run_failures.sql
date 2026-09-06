-- Failure categories for a run, ranked, with sample evidence.
--
-- The grain of dq.validation_failures is one rule violation, which is what makes
-- this a plain GROUP BY. Had the table stored one row per rejected record with
-- its failures in a JSON array, this same question would need a lateral unnest
-- and could not use an index. See ADR-008.
--
-- Parameter: :run_id

SELECT
    vf.rule_name,
    vf.field_name,
    vf.severity,
    count(*)                                    AS failure_count,
    count(DISTINCT vf.raw_record_id)            AS affected_records,
    round(count(*) * 100.0 / sum(count(*)) OVER (), 2) AS share_of_failures_pct,
    min(vf.message)                             AS message,
    min(vf.expected)                            AS expected,
    (array_agg(vf.actual_value ORDER BY vf.failure_id)
        FILTER (WHERE vf.actual_value IS NOT NULL))[1:5] AS sample_actual_values,
    (array_agg(vf.source_record_id ORDER BY vf.failure_id)
        FILTER (WHERE vf.source_record_id IS NOT NULL))[1:5] AS sample_source_records
FROM dq.validation_failures vf
WHERE vf.run_id = :run_id
GROUP BY vf.rule_name, vf.field_name, vf.severity
ORDER BY failure_count DESC, vf.rule_name;
