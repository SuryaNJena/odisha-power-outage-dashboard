{#-
    Timestamp parsing for the two raw Kavach feed formats (see CONTRACT.md):
        'DD-MON-YYYY HH:MM'   e.g. 28-AUG-2026 08:00   (circle schema)
        'DD-MM-YYYY HH:MM'    e.g. 26-08-2026 09:21   (division / emergency schema)

    Both are IST wall-clock strings. They are parsed as timestamp-without-tz and
    then pinned to Asia/Kolkata, so the result is deterministic no matter what
    the Postgres session TimeZone is; the stored value is a UTC instant.

    Robustness: each conversion is guarded by an anchored regex that validates
    day/month/hour ranges, so one malformed feed row yields NULL instead of
    aborting the whole dbt query (Postgres to_timestamp() raises on bad input).
    The numeric branch only runs when the text is *not* month-name format, so a
    bad value never falls through to the wrong parser.

    feed_timestamp_text(): trim, lowercase, unify '/', '.' to '-', collapse
    repeated whitespace. parse_feed_timestamp() expects that normalized text and
    returns timestamptz (or NULL).
-#}
{% macro feed_timestamp_text(expression) -%}
regexp_replace(
        replace(replace(lower(btrim(coalesce({{ expression }}, ''))), '/', '-'), '.', '-'),
        '\s+', ' ', 'g'
    )
{%- endmacro %}

{% macro parse_feed_timestamp(normalized_text) -%}
case
        when {{ normalized_text }} ~ '^(0?[1-9]|[12][0-9]|3[01])-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-[0-9]{4} ([01]?[0-9]|2[0-3]):[0-5][0-9]$'
            then (to_timestamp({{ normalized_text }}, 'DD-MON-YYYY HH24:MI')::timestamp at time zone 'Asia/Kolkata')
        when {{ normalized_text }} ~ '^(0?[1-9]|[12][0-9]|3[01])-(0?[1-9]|1[0-2])-[0-9]{4} ([01]?[0-9]|2[0-3]):[0-5][0-9]$'
            then (to_timestamp({{ normalized_text }}, 'DD-MM-YYYY HH24:MI')::timestamp at time zone 'Asia/Kolkata')
    end
{%- endmacro %}
