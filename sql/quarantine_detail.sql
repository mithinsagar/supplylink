-- Rejected records for a run, with every rule each one broke.
--
-- The two-table quarantine model pays off here: the record's payload is stored
-- once, and its failures are aggregated back onto it, so investigating a
-- rejection needs one query and returns one row per record rather than a
-- cartesian smear of payload copies.
--
-- Parameters: :run_id, :limit

SELECT
    q.quarantine_id,
    q.source_record_id,
    q.ordinal            AS source_line,
    q.failure_count,
    q.payload_truncated,
    q.raw_payload,
    jsonb_agg(
        jsonb_build_object(
            'field',    vf.field_name,
            'rule',     vf.rule_name,
            'expected', vf.expected,
            'actual',   vf.actual_value,
            'message',  vf.message
        )
        ORDER BY vf.failure_id
    )                    AS failures
FROM dq.quarantined_records q
JOIN dq.validation_failures vf ON vf.quarantine_id = q.quarantine_id
WHERE q.run_id = :run_id
GROUP BY q.quarantine_id, q.source_record_id, q.ordinal,
         q.failure_count, q.payload_truncated, q.raw_payload
ORDER BY q.failure_count DESC, q.ordinal
LIMIT :limit;
