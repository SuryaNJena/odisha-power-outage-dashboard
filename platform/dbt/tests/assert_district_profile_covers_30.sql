-- dbt singular test: the district dimension must cover exactly the 30 districts
-- of Odisha (seeds/district_profile.csv). dbt test fails if this returns a row.

select count(*) as district_count
from {{ ref('dim_district') }}
having count(*) <> 30
