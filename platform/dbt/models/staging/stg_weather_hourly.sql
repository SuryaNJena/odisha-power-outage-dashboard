-- Clean hourly weather observations/forecasts per canonical district.
-- One row per (district, observed_at). IST date/hour columns are exposed for
-- daily aggregation without repeating the timezone conversion everywhere.

select
    district::text                                          as district,
    observed_at                                             as observed_at,
    (observed_at at time zone 'Asia/Kolkata')::date         as observed_date,
    temperature_c                                           as temperature_c,
    precipitation_mm                                        as precipitation_mm,
    wind_kph                                                as wind_kph,
    humidity_pct                                            as humidity_pct,
    weather_code                                            as weather_code,
    source                                                  as source,
    fetched_at                                              as fetched_at
from {{ source('raw', 'weather_hourly') }}
where district is not null
  and observed_at is not null
