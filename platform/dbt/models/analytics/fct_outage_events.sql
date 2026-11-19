-- analytics.fct_outage_events — deduplicated outage event history.
--
-- One row per logical outage (raw row_hash), deduped across scrape batches:
--   first_seen / last_seen = batch timestamps when the outage was first/last listed
--   is_active              = still listed in the latest successful scrape batch
--
-- Contract columns only (the API/consumers rely on this exact surface):
--   row_hash, district, start_ts, end_ts, duration_minutes, is_planned,
--   cause_category, voltage, feeder, gss, areas, reason, first_seen, last_seen,
--   is_active
--
-- `district` may be 'Unknown' when neither a district alias nor an exact circle
-- match exists; aggregates that are district-grain filter that bucket out.

select
    row_hash,
    district,
    start_ts,
    end_ts,
    duration_minutes,
    is_planned,
    cause_category,
    voltage,
    feeder,
    gss,
    areas,
    reason,
    first_seen,
    last_seen,
    is_active
from {{ ref('stg_outage_events') }}
