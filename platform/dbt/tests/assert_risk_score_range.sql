-- dbt singular test: risk scores must be probabilities (0..1) and risk_level
-- must agree with the documented thresholds.
-- dbt test fails if this returns any rows.

with snapshot_violations as (

    select
        'district_live_snapshot' as source_model,
        district,
        risk_score,
        null::text as risk_level
    from {{ ref('district_live_snapshot') }}
    where risk_score is null
       or risk_score < 0
       or risk_score > 1

),

risk_violations as (

    select
        'district_risk_daily' as source_model,
        district,
        risk_score,
        risk_level
    from {{ ref('district_risk_daily') }}
    where risk_score is null
       or risk_score < 0
       or risk_score > 1
       or risk_level is null
       or risk_level not in ('low', 'moderate', 'high', 'severe')

)

select * from snapshot_violations
union all
select * from risk_violations
