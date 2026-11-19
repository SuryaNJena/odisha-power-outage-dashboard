-- marts.outage_cause_daily — daily outage rollup by cause taxonomy (all history).
--
-- Contract columns: metric_date, cause_category, outage_count, affected_customers
--
-- `affected_customers` is the estimated number of customer-interruptions for
-- that cause/day (sum of per-outage estimates; a customer interrupted by two
-- different outages is counted twice — this is an interruption count, not a
-- distinct-customer count). IST dates, deduplicated history (one row per outage).
--
-- cause_category is one of: weather | tree_trimming | equipment_failure |
-- maintenance | project_work | line_work | other (see macros/classify_cause.sql).

select
    (e.start_ts at time zone 'Asia/Kolkata')::date       as metric_date,
    e.cause_category,
    count(*)::integer                                    as outage_count,
    round(sum(coalesce(e.affected_customers, 0)))::integer as affected_customers
from {{ ref('stg_outage_events') }} as e
where e.start_ts is not null
group by 1, 2
