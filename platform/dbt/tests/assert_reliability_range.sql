-- dbt singular test: reliability indices must stay in sane ranges.
-- dbt test fails if this returns any rows.
--   ASAI (%)  in [0, 100]
--   SAIDI (min) >= 0, SAIFI >= 0, CAIDI (min) >= 0
-- Also asserts the metric grain (district, metric_date) is unique.

with range_violations as (

    select
        district,
        metric_date,
        'range' as violation,
        saidi_minutes,
        saifi,
        caidi_minutes,
        asai_pct
    from {{ ref('reliability_daily') }}
    where asai_pct is null
       or asai_pct < 0
       or asai_pct > 100
       or saidi_minutes is null
       or saidi_minutes < 0
       or saifi is null
       or saifi < 0
       or caidi_minutes is null
       or caidi_minutes < 0

),

grain_violations as (

    select
        district,
        metric_date,
        'duplicate_grain' as violation,
        count(*)::numeric as saidi_minutes,
        null::numeric as saifi,
        null::numeric as caidi_minutes,
        null::numeric as asai_pct
    from {{ ref('reliability_daily') }}
    group by district, metric_date
    having count(*) > 1

)

select * from range_violations
union all
select * from grain_violations
