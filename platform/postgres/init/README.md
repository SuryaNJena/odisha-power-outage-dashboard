# Postgres init — Odisha outage platform

These files are mounted into the `postgres:16-alpine` container at
`/docker-entrypoint-initdb.d/` and run once, in filename order, when the data
directory is empty. They are idempotent, so they also work when applied manually
to an existing database.

| File | Purpose |
|---|---|
| `01_schemas.sql` | Creates the `airflow` metadata database and the `raw`, `staging`, `analytics`, `marts`, `realtime`, `ml`, `ops`, `seeds` schemas in the `odisha` database. |
| `02_raw.sql` | Creates every contract table in `raw`, `realtime`, `ml` and `ops` (with indexes). Spark/Flink write into these tables, so they are provisioned here instead of by the jobs. |
| `03_reference_seed.sql` | Loads `raw.district_aliases`, `raw.circle_exact`, `raw.district_centroids` and `raw.station_geocodes` from the existing `data/mappings/*.json` files (generated; idempotent `ON CONFLICT DO NOTHING`). |

## Notes for deployment

* `01_schemas.sql` contains one psql meta-command (`\gexec`) because
  `CREATE DATABASE airflow` cannot run inside a transaction block and Postgres
  has no `CREATE DATABASE IF NOT EXISTS`. The official postgres image always
  executes `*.sql` init files with `psql`, so this works out of the box; the
  fallback for a non-psql runner is documented inline in the file.
* The init scripts run as `$POSTGRES_USER` (superuser) on `$POSTGRES_DB=odisha`,
  so all objects are owned by the same role the Airflow/Spark/Go services use.
  No extra `GRANT`s are required.
* The `odisha_bootstrap` Airflow DAG may upsert the same reference rows from
  `data/mappings/*.json`; use the normalization documented at the top of
  `03_reference_seed.sql` so the primary keys conflict cleanly:
  * `district_aliases.alias = regexp_replace(lower(alias), '[^a-z0-9]+', '', 'g')`
  * `circle_exact.circle = upper(trim(circle))`
  * `station_geocodes.station_norm = regexp_replace(lower(station), '[^a-z0-9]+', '', 'g')`
* `raw.user_reports` and `raw.scrape_batches` are append-only / upsert tables for
  the Go API and Airflow loaders; nothing in this directory deletes data.
