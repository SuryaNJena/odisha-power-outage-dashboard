{{ config(severity='warn') }}

-- dbt singular test: marts.map_dots must contain exactly the latest successful
-- batch's rows that resolved to a known district (one dot per resolved outage;
-- unresolved 'Unknown' rows are intentionally excluded from the map). This
-- catches accidental fan-out from the geocode/reference joins.
--
-- severity = warn: a new batch can land between the mart model and the test in a
-- live 30-minute pipeline, so a transient mismatch must not fail the DAG.

with latest_successful_batch as (

    select batch_id
    from {{ source('raw', 'scrape_batches') }}
    where status = 'success'
    order by finished_at desc nulls last, started_at desc
    limit 1

),

expected as (

    select count(*) as row_count
    from {{ ref('stg_official_outages') }} as s
    join latest_successful_batch as b
        on b.batch_id = s.batch_id
    where s.district <> '{{ var("unknown_district", "Unknown") }}'

),

actual as (

    select count(*) as row_count
    from {{ ref('map_dots') }}

)

select
    a.row_count as map_dots_rows,
    e.row_count as expected_rows
from actual as a
cross join expected as e
where a.row_count <> e.row_count
