-- stg_outage_events
-- Deduplicated outage history: one row per row_hash across ALL successful batches.
--
-- The scraper re-lists the same outages in consecutive 30-minute batches, so a
-- logical outage appears once per batch. raw.official_outages.row_hash is stable
-- for the same outage (same hash recipe as process.py), which makes it the
-- natural dedupe key:
--   * first_seen / last_seen = earliest / latest batch start where the row appeared
--   * is_active              = the row is present in the LATEST successful batch
--   * payload columns        = values from the most recent batch (latest wins)
--
-- Only batches with status = 'success' are considered, so partially ingested /
-- failed scrapes never leak into the marts. If no successful batch exists yet the
-- model is simply empty (cold start is safe).
--
-- Consumers:
--   analytics.fct_outage_events   (contract columns only)
--   analytics.reliability_daily   (needs customers / affected_customers)
--   analytics.district_live_snapshot
--   marts.outage_cause_daily

with successful_rows as (

    select *
    from {{ ref('stg_official_outages') }}
    where batch_status = 'success'
      -- Published-history window (default 120 days, must cover the 90-day
      -- reliability window). raw.official_outages keeps the full history; this
      -- bounds the cost of the 30-minute dbt rebuild as the raw table grows.
      and batch_started_at >= (
          now() - interval '{{ var("history_lookback_days", 120) }} days'
      )

),

latest_successful_batch as (

    select batch_id
    from {{ source('raw', 'scrape_batches') }}
    where status = 'success'
    order by finished_at desc nulls last, started_at desc
    limit 1

),

windowed as (

    select
        s.*,
        row_number() over (
            partition by s.row_hash
            order by s.batch_started_at desc nulls last, s.id desc
        ) as row_rank,
        min(s.batch_started_at) over (partition by s.row_hash) as first_seen,
        max(s.batch_started_at) over (partition by s.row_hash) as last_seen
    from successful_rows as s

)

select
    w.id,
    w.row_hash,
    w.district,
    w.zone,
    w.customers,
    w.start_ts,
    w.end_ts,
    w.duration_minutes,
    w.is_planned,
    w.cause_category,
    w.voltage,
    w.feeder,
    w.gss,
    w.areas,
    w.reason,
    w.circle,
    w.division,
    w.schema_type,
    w.start_raw,
    w.end_raw,
    w.lat,
    w.lon,
    w.geocoded,
    w.affected_customers,
    w.first_seen,
    w.last_seen,
    exists (
        select 1
        from successful_rows as current_rows
        where current_rows.row_hash = w.row_hash
          and current_rows.batch_id = (select batch_id from latest_successful_batch)
    ) as is_active
from windowed as w
where w.row_rank = 1
