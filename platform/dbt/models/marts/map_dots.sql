-- marts.map_dots — one row per outage in the LATEST SUCCESSFUL scrape batch.
-- This is the map payload: the Go API /api/mapdata serializes these rows (and the
-- Airflow mart-publish task writes them to /opt/odisha/data/mart/outages.json).
--
-- Contract columns:
--   district, circle, division, gss, voltage, feeder, start_text, end_text,
--   outage_date (date), reason, areas, lat, lon, geocoded (bool),
--   is_planned (bool), duration_minutes (numeric), affected_customers (int),
--   cause_category (text), etr (timestamptz)
--
-- start_text / end_text are the ORIGINAL feed strings (e.g. '28-AUG-2026 08:00',
-- '26-08-2026 09:21') because the frontend parses both formats itself.
-- etr = expected restoration time (= parsed end timestamp, UTC instant).
--
-- Rows whose district could not be resolved ('Unknown') are excluded from the
-- map mart (the legacy process.py also skipped them, and the map must not render
-- a bogus district). They remain in analytics.fct_outage_events for auditing.
--
-- If the latest successful batch has zero rows the mart is empty (not an error);
-- the loader marks empty scrapes as success on purpose so this is expected.
-- If no successful batch exists yet, the mart is empty (cold start is safe).

with latest_successful_batch as (

    select batch_id
    from {{ source('raw', 'scrape_batches') }}
    where status = 'success'
    order by finished_at desc nulls last, started_at desc
    limit 1

)

select
    o.district,
    o.circle,
    o.division,
    o.gss,
    o.voltage,
    o.feeder,
    o.start_raw                                          as start_text,
    o.end_raw                                            as end_text,
    (o.start_ts at time zone 'Asia/Kolkata')::date       as outage_date,
    o.reason,
    o.areas,
    o.lat,
    o.lon,
    o.geocoded,
    o.is_planned,
    o.duration_minutes,
    o.affected_customers,
    o.cause_category,
    o.end_ts                                             as etr
from {{ ref('stg_official_outages') }} as o
join latest_successful_batch as b
    on b.batch_id = o.batch_id
where o.district <> '{{ var("unknown_district", "Unknown") }}'
