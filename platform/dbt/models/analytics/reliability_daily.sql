-- analytics.reliability_daily — per district x day reliability indices.
--
-- Contract columns: district, metric_date, saidi_minutes, saifi, caidi_minutes,
--                   asai_pct, outage_count, customers_affected
--
-- Definitions (IEEE 1366, estimated customer counts; see platform/dbt/README.md):
--   C_i          = estimated customers interrupted by outage i (district_profile
--                  customers x per-outage share; see estimated_affected_customers)
--   N            = district_profile.customers (total customers in the district)
--   d_i          = outage duration in minutes, capped at 1440 per event so one
--                  multi-day row cannot distort a single day's index
--   SAIDI (min)  = SUM_i (d_i * C_i) / N        -- customer-minutes / customers
--                  (divide by 60 for SAIDI hours)
--   SAIFI        = SUM_i (C_i) / N              -- interruptions per customer
--   CAIDI (min)  = SAIDI / SAIFI                -- avg interruption duration
--   ASAI (%)     = 100 * (1 - SAIDI / 1440)     -- daily availability, floored at 0
--
-- One row per district per day for the trailing reliability_lookback_days
-- (default 90) so the API's 30-day window is always fully covered, including
-- zero-outage days (SAIDI 0, SAIFI 0, CAIDI 0, ASAI 100).
--
-- Outages are attributed to the IST date of start_ts (the display timezone);
-- planned and unplanned outages are both counted (SAIDI/SAIFI convention).

{% set unknown_district = var('unknown_district', 'Unknown') %}

with events as (

    select
        e.district,
        (e.start_ts at time zone 'Asia/Kolkata')::date as metric_date,
        least(coalesce(e.duration_minutes, 0), 1440)  as duration_minutes,
        coalesce(e.affected_customers, 0)             as affected_customers,
        e.customers
    from {{ ref('stg_outage_events') }} as e
    where e.start_ts is not null
      and e.district is not null
      and e.district <> '{{ unknown_district }}'
      and e.customers is not null

),

daily as (

    select
        district,
        metric_date,
        count(*)                                   as outage_count,
        max(customers)                             as customers,
        sum(affected_customers)                    as affected_customers_sum,
        sum(duration_minutes * affected_customers) as customer_minutes
    from events
    group by district, metric_date

),

spine as (

    select
        d.district,
        d.customers,
        ((now() at time zone 'Asia/Kolkata')::date - gs.n) as metric_date
    from {{ ref('dim_district') }} as d
    cross join generate_series(0, {{ var('reliability_lookback_days', 90) - 1 }}) as gs(n)

),

joined as (

    select
        s.district,
        s.metric_date,
        s.customers,
        coalesce(dd.outage_count, 0)            as outage_count,
        coalesce(dd.affected_customers_sum, 0)  as affected_customers_sum,
        coalesce(dd.customer_minutes, 0)        as customer_minutes
    from spine as s
    left join daily as dd
        on dd.district = s.district
       and dd.metric_date = s.metric_date

)

select
    district,
    metric_date,
    round(customer_minutes / nullif(customers, 0), 3)                       as saidi_minutes,
    round(affected_customers_sum::numeric / nullif(customers, 0), 4)        as saifi,
    case
        when coalesce(affected_customers_sum, 0) > 0
            then round(
                (customer_minutes / nullif(customers, 0))
                / (affected_customers_sum::numeric / nullif(customers, 0)),
                2
            )
        else 0
    end                                                                      as caidi_minutes,
    round(
        100.0 * greatest(0.0, 1.0 - coalesce(customer_minutes / nullif(customers, 0), 0) / 1440.0),
        3
    )                                                                        as asai_pct,
    outage_count::integer                                                    as outage_count,
    least(customers, round(affected_customers_sum::numeric)::numeric)::integer as customers_affected
from joined
