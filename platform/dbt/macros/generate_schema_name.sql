{#-
    Schema routing macro: use the model/seed `+schema` value verbatim instead of
    dbt's default `target.schema + "_" + custom_schema` concatenation.

    Resulting physical schemas (per CONTRACT.md):
      models/staging    -> staging
      models/analytics  -> analytics
      models/marts      -> marts
      seeds             -> seeds
    Models without a custom schema fall back to the profile schema (analytics).
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}
        {{ default_schema | trim }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
