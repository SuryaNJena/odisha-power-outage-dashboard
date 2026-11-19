{#-
    Estimated number of customers interrupted by one official outage row.

    The KVACH feed has no customer count, so it is estimated from the district's
    customer base (seeds/district_profile.csv) and the outage scope signals:

        share     = min(1, voltage_share * area_factor)
        affected  = min(district_customers,
                        max(1, round(district_customers * share)))

    voltage_share by voltage level (share of a district's customers on one
    feeder/equipment of that level; deliberately conservative — most outages are
    a single 11kV feeder, not the whole district):
        66/132/220/400 kV -> 2.0%   (bulk transmission / grid substation)
        33 kV             -> 1.0%
        11 kV             -> 0.35%
        LT / sub-11kV     -> 0.10%
        unknown/missing   -> 0.20%

    area_factor widens the estimate when the feed lists many affected areas:
        min(2, 1 + 0.02 * number_of_comma_separated_areas)

    This feeds marts.map_dots.affected_customers, reliability_daily (SAIDI/SAIFI/
    CAIDI/ASAI) and marts.outage_cause_daily — documented in platform/dbt/README.md.
-#}
{% macro estimated_affected_customers(customers_expression, voltage_expression, areas_expression) -%}
case
        when {{ customers_expression }} is null then null
        else least(
            {{ customers_expression }},
            greatest(
                1,
                round(
                    {{ customers_expression }} * least(
                        1.0,
                        (case
                            when upper(coalesce({{ voltage_expression }}, '')) ~ '^(66|132|220|400)'
                                then 0.0200
                            when upper(coalesce({{ voltage_expression }}, '')) like '33%'
                                then 0.0100
                            when upper(coalesce({{ voltage_expression }}, '')) like '11%'
                                then 0.0035
                            when upper(coalesce({{ voltage_expression }}, '')) like '%LT%'
                                then 0.0010
                            else 0.0020
                        end)
                        * least(
                            2.0,
                            1.0 + 0.02 * (
                                length(coalesce({{ areas_expression }}, ''))
                                - length(replace(coalesce({{ areas_expression }}, ''), ',', ''))
                            )
                        )
                    )
                )
            )
        )
    end
{%- endmacro %}
