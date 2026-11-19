-- analytics.dim_district — district dimension used by the API (/api/reliability,
-- /api/risk, /api/realtime joins) and by every district-grain aggregate.
-- Contract columns: district, zone, customers, population, urban_share, lat, lon.
-- 30 canonical Odisha districts come from seeds/district_profile.csv.

select
    district,
    zone,
    customers,
    population,
    urban_share,
    lat,
    lon
from {{ ref('stg_district_profile') }}
