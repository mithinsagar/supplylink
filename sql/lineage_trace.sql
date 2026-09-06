-- End-to-end lineage for one SKU: warehouse value back to the source bytes.
--
-- This is the query that answers "where did this number come from?". It walks
-- the chain the schema was designed to preserve:
--
--   canonical version -> raw source record -> pipeline run -> contract version
--                     -> reconciliation verdict -> quality report
--
-- Every hop is a foreign key, so none of it depends on convention or on log
-- retention.
--
-- Parameter: :sku

SELECT
    pv.canonical_sku,
    pv.version_number,
    pv.unit_price,
    pv.currency,
    pv.product_name,
    pv.effective_from,
    pv.effective_to,

    -- where it came from
    pv.source_id,
    pv.source_record_id,
    raw.ordinal                AS source_line,
    raw.payload                AS source_payload,
    raw.payload_hash,
    raw.ingested_at,

    -- which run produced it, under which contract
    run.run_id,
    run.trigger,
    run.started_at             AS run_started_at,
    run.status                 AS run_status,
    cv.version                 AS contract_version,
    cv.content_hash            AS contract_hash,

    -- and how that run reconciled
    run.reconciliation_status,
    recon.content_checksum_status,
    qr.status                  AS quality_report_status
FROM core.product_versions pv
JOIN staging.raw_records     raw ON raw.raw_record_id = pv.raw_record_id
JOIN meta.pipeline_runs      run ON run.run_id = pv.run_id
JOIN meta.contract_versions  cv  ON cv.contract_version_id = run.contract_version_id
LEFT JOIN LATERAL (
    SELECT rr.status AS content_checksum_status
    FROM dq.reconciliation_results rr
    WHERE rr.run_id = run.run_id AND rr.check_name = 'content_checksum'
) recon ON TRUE
LEFT JOIN dq.quality_reports qr ON qr.run_id = run.run_id
WHERE pv.canonical_sku = :sku
ORDER BY pv.version_number;
