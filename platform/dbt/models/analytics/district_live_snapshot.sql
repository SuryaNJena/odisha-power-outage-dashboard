-- analytics.district_live_snapshot — current district state for the API.
--
-- Contract columns: district, active_outages, affected_customers,
--                   avg_etr_minutes, risk_score, updated_at
--
-- `active_outages` comes from analytics.fct_outage_events semantics (rows still
-- listed in the latest successful scrape batch). The risk score blends official
-- outage pressure with near-real-time user-report pressure from Spark streaming
-- (realtime.district_live_metrics, read-only) so the dashboard still works when
-- Spark has not written yet (left join -> 0 report pressure).
--
-- risk_score (0..1) = 0.45 * min(1, active_outages / 10)
--                   + 0.35 * min(1, affected_customers / district_customers)
--                   + 0.20 * min(1, reports_last_15min / 10)
--
-- One row per canonical district (30), including districts with no active outages
-- (0 outages, 0 affected, 0 avg ETR, risk 0).

{% set unknown_district = var('unknown_district', 'Unknown') %}

with active as (

    select
        e.district,
        count(*)                                   as active_outages,
        sum(coalesce(e.affected_customers, 0))      as affected_customers_sum,
        avg(
            case
                when e.end_ts is not null
                    then greatest(extract(epoch from (e.end_ts - now())) / 60.0, 0)
            end
        )                                          as avg_etr_minutes
    from {{ ref('stg_outage_events') }} as e
    where e.is_active
      and e.district is not null
      and e.district <> '{{ unknown_district }}'
    group by e.district

),

reports as (

    select
        m.district,
        m.report_count
    from {{ source('realtime', 'district_live_metrics') }} as m

),

snapshot as (

    select
        d.district,
        coalesce(a.active_outages, 0)                                as active_outages,
        least(d.customers, round(coalesce(a.affected_customers_sum, 0)::numeric))::integer
                                                                     as affected_customers,
        coalesce(round(a.avg_etr_minutes, 1), 0)                     as avg_etr_minutes,
        least(
            1.0,
            round(
                0.45 * least(1.0, coalesce(a.active_outages, 0)::numeric / 10.0)
              + 0.35 * least(1.0, coalesce(a.affected_customers_sum, 0)::numeric / nullif(d.customers, 0))
              + 0.20 * least(1.0, coalesce(r.report_count, 0)::numeric / 10.0),
                3
            )
        )                                                            as risk_score,
        now()                                                        as updated_at
    from {{ ref('dim_district') }} as d
    left join active as a
        on a.district = d.district
    left join reports as r
        on r.district = d.district

)

select
    district,
    active_outages::integer,
    affected_customers,
    avg_etr_minutes,
    risk_score,
    updated_at
from snapshot
