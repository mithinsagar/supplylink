-- Inventory value by supplier, from current canonical state.
--
-- Included because a warehouse that cannot answer a business question is just a
-- pile of tables. It joins the Type 2 dimension's open versions to the latest
-- inventory fact, which is the join every consumer of this model will write.
--
-- No parameters.

SELECT
    s.supplier_id,
    s.supplier_name,
    s.country,
    count(*)                                              AS active_skus,
    count(*) FILTER (WHERE pc.status = 'DISCONTINUED')    AS discontinued_skus,
    sum(pc.quantity_available)                            AS units_on_hand,
    round(sum(pc.unit_price * coalesce(pc.quantity_available, 0)), 2) AS inventory_value,
    round(avg(pc.unit_price), 2)                          AS avg_unit_price,
    count(DISTINCT pc.category)                           AS categories_supplied,
    count(DISTINCT pc.source_id)                          AS mastering_sources
FROM core.products_current pc
JOIN core.suppliers s ON s.supplier_id = pc.supplier_id
GROUP BY s.supplier_id, s.supplier_name, s.country
ORDER BY inventory_value DESC NULLS LAST;
