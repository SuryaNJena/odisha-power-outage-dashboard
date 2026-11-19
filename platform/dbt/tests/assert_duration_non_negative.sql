-- dbt singular test: contract assertion duration_minutes >= 0.
-- A dbt test fails when this query returns any rows.
-- Covers both the deduplicated fact and the published map mart.

with violations as (

    select
        'fct_outage_events' as source_model,
        row_hash            as record_key,
        duration_minutes
    from {{ ref('fct_outage_events') }}
    where duration_minutes is not null
      and duration_minutes < 0

    union all

    select
        'map_dots' as source_model,
        md5(coalesce(district, '') || '|' || coalesce(start_text, '') || '|' || coalesce(feeder, '')) as record_key,
        duration_minutes
    from {{ ref('map_dots') }}
    where duration_minutes is not null
      and duration_minutes < 0

)

select * from violations
