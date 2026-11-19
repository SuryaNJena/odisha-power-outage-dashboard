-- stg_official_outages
-- One row per row in raw.official_outages (i.e. per batch x row_hash), enriched:
--   * normalized text fields (lower alnum, process.py._norm semantics)
--   * IST feed timestamp parsing (DD-MON-YYYY HH:MM / DD-MM-YYYY HH:MM)
--   * duration in minutes (>= 0; midnight wrap fixed for the same-day division schema)
--   * is_planned (schema_type = 'circle' -> planned outage notification)
--   * cause_category taxonomy
--   * district resolution (process.py semantics, see below)
--   * geocoding: raw.station_geocodes -> raw.district_centroids -> district_profile lat/lon
--   * estimated affected customers (macro estimated_affected_customers)
--
-- District resolution mirrors process.py.resolve_district():
--   1. normalize every admin field with regexp_replace(lower(x),'[^a-z0-9]+','','g')
--   2. for fields in priority order division -> subdivision -> section -> circle,
--      find the LONGEST alias that is a substring of the normalized field
--      (raw.district_aliases.alias is stored pre-normalized; ORDER BY length DESC)
--   3. fall back to an exact match on the upper-case circle (raw.circle_exact)
--   4. fall back to 'Unknown'
-- This is the same order as process.py, so the SQL mart and the legacy JSON mart
-- resolve a given feed row to the same district.

with source_rows as (

    select
        o.id,
        o.batch_id,
        o.row_hash,
        o.source_file,
        o.schema_type,
        o.circle,
        o.division,
        o.subdivision,
        o.section,
        o.otype,
        o.gss,
        o.voltage,
        o.feeder,
        o.start_raw,
        o.end_raw,
        o.reason,
        o.areas,
        o.remarks,
        o.ingested_at,
        b.started_at  as batch_started_at,
        b.finished_at as batch_finished_at,
        b.status      as batch_status
    from {{ source('raw', 'official_outages') }} as o
    left join {{ source('raw', 'scrape_batches') }} as b
        on b.batch_id = o.batch_id

),

normalized as (

    select
        s.*,
        {{ normalize_text('s.division') }}        as division_norm,
        {{ normalize_text('s.subdivision') }}     as subdivision_norm,
        {{ normalize_text('s.section') }}         as section_norm,
        {{ normalize_text('s.circle') }}          as circle_norm,
        {{ normalize_station('s.gss') }}          as station_norm,
        upper(btrim(coalesce(s.circle, '')))      as circle_upper,
        {{ feed_timestamp_text('s.start_raw') }}  as start_text_norm,
        {{ feed_timestamp_text('s.end_raw') }}    as end_text_norm
    from source_rows as s

),

alias_matches as (

    select
        n.*,
        (
            select a.district
            from {{ source('raw', 'district_aliases') }} as a
            where position(a.alias in n.division_norm) > 0
            order by length(a.alias) desc, a.alias
            limit 1
        ) as division_district,
        (
            select a.district
            from {{ source('raw', 'district_aliases') }} as a
            where position(a.alias in n.subdivision_norm) > 0
            order by length(a.alias) desc, a.alias
            limit 1
        ) as subdivision_district,
        (
            select a.district
            from {{ source('raw', 'district_aliases') }} as a
            where position(a.alias in n.section_norm) > 0
            order by length(a.alias) desc, a.alias
            limit 1
        ) as section_district,
        (
            select a.district
            from {{ source('raw', 'district_aliases') }} as a
            where position(a.alias in n.circle_norm) > 0
            order by length(a.alias) desc, a.alias
            limit 1
        ) as circle_district
    from normalized as n

),

resolved as (

    select
        m.*,
        coalesce(
            m.division_district,
            m.subdivision_district,
            m.section_district,
            m.circle_district,
            (
                select ce.district
                from {{ source('raw', 'circle_exact') }} as ce
                where ce.circle = m.circle_upper
                limit 1
            ),
            '{{ var("unknown_district", "Unknown") }}'
        ) as district
    from alias_matches as m

),

parsed as (

    select
        r.*,
        {{ parse_feed_timestamp('r.start_text_norm') }} as start_ts_parsed,
        {{ parse_feed_timestamp('r.end_text_norm') }}   as end_ts_parsed
    from resolved as r

),

timed as (

    select
        p.*,
        p.start_ts_parsed as start_ts,
        -- The division/emergency feed has a single date and two times; an end
        -- time earlier than the start time means the outage crossed midnight.
        -- Add a day instead of publishing a negative duration. Any remaining
        -- negative value (bad data) is clamped to 0 by the duration expression.
        case
            when p.end_ts_parsed is not null
             and p.start_ts_parsed is not null
             and p.end_ts_parsed < p.start_ts_parsed
             and p.schema_type = 'division'
                then p.end_ts_parsed + interval '1 day'
            else p.end_ts_parsed
        end as end_ts
    from parsed as p

),

measured as (

    select
        t.*,
        case
            when t.start_ts is not null and t.end_ts is not null
                then round(
                    greatest(extract(epoch from (t.end_ts - t.start_ts)) / 60.0, 0)::numeric,
                    1
                )
        end as duration_minutes,
        (coalesce(t.schema_type, '') = 'circle') as is_planned,
        {{ classify_cause('t.reason', 't.otype') }} as cause_category
    from timed as t

),

enriched as (

    select
        m.*,
        sg.lat as geocode_lat,
        sg.lon as geocode_lon,
        coalesce(sg.lat, dc.lat, dp.lat)          as lat,
        coalesce(sg.lon, dc.lon, dp.lon)          as lon,
        (sg.lat is not null and sg.lon is not null) as geocoded,
        dp.customers                               as customers,
        dp.zone                                    as zone
    from measured as m
    left join {{ source('raw', 'station_geocodes') }} as sg
        on sg.district = m.district
       and sg.station_norm = m.station_norm
    left join {{ source('raw', 'district_centroids') }} as dc
        on dc.district = m.district
    left join {{ ref('district_profile') }} as dp
        on dp.district = m.district

),

scored as (

    select
        e.*,
        {{ estimated_affected_customers('e.customers', 'e.voltage', 'e.areas') }}::integer as affected_customers
    from enriched as e

)

select
    id,
    batch_id,
    batch_started_at,
    batch_finished_at,
    batch_status,
    row_hash,
    source_file,
    schema_type,
    circle,
    division,
    subdivision,
    section,
    otype,
    gss,
    voltage,
    feeder,
    start_raw,
    end_raw,
    start_ts,
    end_ts,
    duration_minutes,
    is_planned,
    cause_category,
    district,
    zone,
    customers,
    lat,
    lon,
    geocoded,
    affected_customers,
    reason,
    areas,
    remarks,
    ingested_at
from scored
