{#-
    Cause taxonomy used by fct_outage_events.cause_category,
    marts.map_dots.cause_category and marts.outage_cause_daily.

    The feed's free-text reason (and, for the division schema, the outage type
    prefix, e.g. "Emergency Shutdown - <cause>") is bucketed by keyword. Order
    matters: weather/tree/equipment signals are checked before generic
    maintenance/line keywords, and planned-work keywords come after failure
    keywords so e.g. "Emergency Shutdown - Burnt Jumper" is equipment_failure.

    Allowed values (enforced by accepted_values tests):
      weather | tree_trimming | equipment_failure | maintenance |
      project_work | line_work | other
-#}
{% macro classify_cause(reason_expression, otype_expression) -%}
{%- set cause_text -%}
lower(coalesce({{ reason_expression }}, '') || ' ' || coalesce({{ otype_expression }}, ''))
{%- endset -%}
case
        when {{ cause_text }} ~ '(rain|monsoon|storm|cyclone|flood|lightning|thunder|weather|wind damage)'
            then 'weather'
        when {{ cause_text }} ~ '(tree|vegetation|bamboo|branch|trimming|devegetation)'
            then 'tree_trimming'
        when {{ cause_text }} ~ '(burnt|burn|damaged|damage|fuse|insulator|relay|transformer|dtr|jumper|snapped|snap|fault|breakdown|blast|punctur|repair|replacement|meter)'
            then 'equipment_failure'
        when {{ cause_text }} ~ '(maintenance|preventive|inspection|shutdown)'
            then 'maintenance'
        when {{ cause_text }} ~ '(project|new |erection|stringing|charging|installation|connect|disconnect|load change|shifting|shift|consumer|cooping|augmentation|upgrade|renovation|work in vicinity|vicinity)'
            then 'project_work'
        when {{ cause_text }} ~ '(line|pole|conductor|feeder|overhead|tower|cross arm|crossarm)'
            then 'line_work'
        else 'other'
    end
{%- endmacro %}
