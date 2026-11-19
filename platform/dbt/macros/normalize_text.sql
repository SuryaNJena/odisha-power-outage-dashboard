{#-
    Text normalization shared by district resolution and geocode lookups.

    Mirrors process.py._norm():
        re.sub(r"[^a-z0-9]+", "", text.lower())

    Used for:
      * raw.district_aliases.alias  (aliases are stored pre-normalized)
      * raw.official_outages.circle/division/subdivision/section match fields
      * raw.station_geocodes.station_norm / raw.official_outages.gss
-#}
{% macro normalize_text(expression) -%}
regexp_replace(lower(coalesce({{ expression }}, '')), '[^a-z0-9]+', '', 'g')
{%- endmacro %}

{#- Same lower-alnum normalization used for station/GSS names. -#}
{% macro normalize_station(expression) -%}
{{ normalize_text(expression) }}
{%- endmacro %}
