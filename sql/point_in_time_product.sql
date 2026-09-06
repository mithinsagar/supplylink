-- What did this SKU look like at a given instant?
--
-- The question SCD Type 2 exists to answer. Because core.product_versions
-- carries a GiST exclusion constraint forbidding overlapping validity periods
-- for a SKU, this predicate returns at most one row by construction — the
-- "pick the latest one and hope" that usually accompanies temporal queries is
-- unnecessary here.
--
-- Half-open interval [effective_from, effective_to): a version that closes at
-- T is not in effect at T, and the version that opens at T is. That is what
-- makes the boundary unambiguous when two versions abut exactly, which they
-- always do — a load closes and opens at the same instant.
--
-- Parameters: :sku, :as_of
-- Index used: product_versions_pit_idx (canonical_sku, effective_from DESC)

SELECT
    pv.canonical_sku,
    pv.version_number,
    pv.product_name,
    pv.category,
    pv.supplier_id,
    s.supplier_name,
    pv.unit_price,
    pv.currency,
    pv.status,
    pv.effective_from,
    pv.effective_to,
    pv.is_current,
    pv.source_id,
    pv.run_id
FROM core.product_versions pv
JOIN core.suppliers s USING (supplier_key)
WHERE pv.canonical_sku = :sku
  AND pv.effective_from <= :as_of
  AND (pv.effective_to > :as_of OR pv.effective_to IS NULL);
