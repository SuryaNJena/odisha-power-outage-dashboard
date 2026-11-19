-- analytics.district_risk_daily — weather-driven outage risk per district x day.
--
-- Contract columns: district, risk_date, risk_score, risk_level, max_precip_mm,
--                   max_wind_kph, mean_temp_c, expected_outages
--
-- Sources:
--   raw.weather_hourly  aggregated to district x IST day (max precip, max wind,
--                       mean temperature); future days use the forecast hours that
--                       Airflow's weather ingest writes into the same table.
--   outage history      expected_outages = average daily outage count over the
--                       previous 14 days (dbt history, IST dates).
--
-- risk_score (0..1) = 0.30 * min(1, max_precip_mm / 40)
--                   + 0.25 * min(1, max_wind_kph / 50)
--                   + 0.10 * min(1, max(0, mean_temp_c - 32) / 8)
--                   + 0.35 * min(1, expected_outages / 8)
--
-- risk_level thresholds (kept in lock-step with the Go API + Spark):
--   severe >= 0.75, high >= 0.50, moderate >= 0.25, low < 0.25.
-- One row per district x day for the trailing risk_past_days (30) plus
-- risk_forecast_days (3) ahead, so /api/risk can serve "today + forecast".
-- Districts/days without weather or history are still emitted with 0 components.

{% set unknown_district = var('unknown_district', 'Unknown') %}

with weather as (

    select
        w.district,
        (w.observed_at at time zone 'Asia/Kolkata')::date as risk_date,
        max(w.precipitation_mm)  as max_precip_mm,
        max(w.wind_kph)          as max_wind_kph,
        avg(w.temperature_c)     as mean_temp_c
    from {{ source('raw', 'weather_hourly') }} as w
    where w.district is not null
      and w.observed_at is not null
    group by 1, 2

),

outage_history as (

    select
        e.district,
        (e.start_ts at time zone 'Asia/Kolkata')::date as risk_date,
        count(*) as outage_count
    from {{ ref('stg_outage_events') }} as e
    where e.start_ts is not null
      and e.district is not null
      and e.district <> '{{ unknown_district }}'
    group by 1, 2

),

spine as (

    select
        d.district,
        ((now() at time zone 'Asia/Kolkata')::date + gs.days_offset) as risk_date
    from {{ ref('dim_district') }} as d
    cross join generate_series(
        -{{ var('risk_past_days', 30) }},
        {{ var('risk_forecast_days', 3) }}
    ) as gs(days_offset)

),

expected as (

    select
        s.district,
        s.risk_date,
        (
            select avg(h.outage_count)
            from outage_history as h
            where h.district = s.district
              and h.risk_date between s.risk_date - 14 and s.risk_date - 1
        ) as expected_outages_avg
    from spine as s

),

scored as (

    select
        x.district,
        x.risk_date,
        w.max_precip_mm,
        w.max_wind_kph,
        w.mean_temp_c,
        coalesce(x.expected_outages_avg, 0) as expected_outages_avg,
        least(
            1.0,
            round(
                0.30 * least(1.0, coalesce(w.max_precip_mm, 0)::numeric / 40.0)
              + 0.25 * least(1.0, coalesce(w.max_wind_kph, 0)::numeric / 50.0)
              + 0.10 * least(1.0, greatest(0, coalesce(w.mean_temp_c, 25) - 32)::numeric / 8.0)
              + 0.35 * least(1.0, coalesce(x.expected_outages_avg, 0)::numeric / 8.0),
                3
            )
        ) as risk_score
    from expected as x
    left join weather as w
        on w.district = x.district
       and w.risk_date = x.risk_date

)

select
    district,
    risk_date,
    risk_score,
    case
        when risk_score >= 0.75 then 'severe'
        when risk_score >= 0.50 then 'high'
        when risk_score >= 0.25 then 'moderate'
        else 'low'
    end                                              as risk_level,
    max_precip_mm,
    max_wind_kph,
    round(mean_temp_c::numeric, 1)                    as mean_temp_c,
    round(expected_outages_avg)::integer              as expected_outages
from scored
