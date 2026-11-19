-- Clean view over seeds/district_profile.csv: one row per canonical Odisha
-- district with the dimensions/weights used by the API and reliability models.
-- `customers` is an estimate of electricity consumers (see seeds README comment
-- in the CSV generation notes and platform/dbt/README.md).

select
    district::text                as district,
    zone::text                    as zone,
    customers::integer            as customers,
    population::integer           as population,
    urban_share::numeric          as urban_share,
    lat::double precision         as lat,
    lon::double precision         as lon
from {{ ref('district_profile') }}
