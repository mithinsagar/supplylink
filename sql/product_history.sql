-- The full version history of a SKU, with what changed at each step.
--
-- LAG over the previous version turns a list of versions into a changelog:
-- rather than making the reader diff adjacent rows by eye, each row states
-- which tracked attributes moved. `changed_attributes` is built by comparing
-- each attribute with its predecessor, so version 1 correctly reports no
-- changes rather than reporting everything as new.
--
-- Parameter: :sku

WITH versions AS (
    SELECT
        pv.canonical_sku,
        pv.version_number,
        pv.product_name,
        pv.category,
        pv.supplier_id,
        pv.unit_price,
        pv.currency,
        pv.status,
        pv.effective_from,
        pv.effective_to,
        pv.is_current,
        pv.source_id,
        pv.run_id,
        pv.record_hash,
        lag(pv.product_name) OVER w AS prev_product_name,
        lag(pv.category)     OVER w AS prev_category,
        lag(pv.supplier_id)  OVER w AS prev_supplier_id,
        lag(pv.unit_price)   OVER w AS prev_unit_price,
        lag(pv.currency)     OVER w AS prev_currency,
        lag(pv.status)       OVER w AS prev_status
    FROM core.product_versions pv
    WHERE pv.canonical_sku = :sku
    WINDOW w AS (PARTITION BY pv.canonical_sku ORDER BY pv.version_number)
)
SELECT
    canonical_sku,
    version_number,
    product_name,
    category,
    supplier_id,
    unit_price,
    currency,
    status,
    effective_from,
    effective_to,
    is_current,
    source_id,
    run_id,
    -- Elapsed time the version was in effect; NULL while still open.
    effective_to - effective_from AS duration,
    array_remove(ARRAY[
        CASE WHEN prev_product_name IS DISTINCT FROM product_name
                  AND version_number > 1 THEN 'product_name' END,
        CASE WHEN prev_category     IS DISTINCT FROM category
                  AND version_number > 1 THEN 'category' END,
        CASE WHEN prev_supplier_id  IS DISTINCT FROM supplier_id
                  AND version_number > 1 THEN 'supplier_id' END,
        CASE WHEN prev_unit_price   IS DISTINCT FROM unit_price
                  AND version_number > 1 THEN 'unit_price' END,
        CASE WHEN prev_currency     IS DISTINCT FROM currency
                  AND version_number > 1 THEN 'currency' END,
        CASE WHEN prev_status       IS DISTINCT FROM status
                  AND version_number > 1 THEN 'status' END
    ], NULL) AS changed_attributes,
    prev_unit_price,
    CASE
        WHEN version_number = 1 OR prev_unit_price IS NULL OR prev_unit_price = 0 THEN NULL
        ELSE round((unit_price - prev_unit_price) * 100 / prev_unit_price, 2)
    END AS price_change_pct
FROM versions
ORDER BY version_number;
