# Feature map — what the platform adds and where the ideas come from

The original dashboard showed planned outages from the DISCOM feed. The platform
adds near-real-time crowd reports, reliability analytics and weather-driven risk.
Several features are adapted from mature outage services outside India; the table
below records the inspiration and how each idea is implemented here.

## Implemented features

| # | Feature | What the user sees | Implementation | Inspired by |
|---|---|---|---|---|
| 1 | **Crowd outage reports** | “Report an outage” form on `/` and `/live`; reports flow to the NRT dashboard within seconds | `POST /api/report` → Kafka `user-reports` + `raw.user_reports` | PowerOutage.us crowd/utility cross-check; Energex “report an outage” |
| 2 | **Unconfirmed-report index** | Per-district badge: reports in the last 15 min minus official active outages | Spark Structured Streaming `realtime.district_live_metrics.unconfirmed_reports` | PowerOutage.us reconciles customer reports against utility data |
| 3 | **Estimated restoration time (ETR)** | ETR per outage (`etr`) and average ETR per district | `marts.map_dots.etr` from the parsed feed end time; `analytics.district_live_snapshot.avg_etr_minutes` | UK Power Networks live power-cut list (“we estimate your power back on”); Energex outage finder |
| 4 | **Customers affected** | Customers affected per outage / district | dbt macro `estimated_affected_customers` (district customer base × voltage/area share) | PowerOutage.us “customers out” counters |
| 5 | **Reliability indices** | `/reliability`: SAIDI, SAIFI, CAIDI, ASAI by district and trend | dbt `analytics.reliability_daily` (IEEE 1366 definitions, 90-day spine) | UK/US DNO regulatory reliability reporting (SAIDI/SAIFI/CAIDI) |
| 6 | **Weather-driven risk forecast** | `/api/risk` + risk badges; next-24h expected outages | dbt `analytics.district_risk_daily` (Open-Meteo precipitation/wind/heat) and Spark `ml.district_risk_forecast` | Storm-outage forecasting used by US/UK utilities |
| 7 | **Live alert feed** | `/live` alert stream with severity colours | Flink SQL 10-minute tumbling windows per district → `outage-alerts` → Spark → `realtime.alerts` | Utility “active incidents” feeds |
| 8 | **Per-minute report trend** | `/live` sparkline of reports/min | Spark 1-minute windows → `realtime.report_minute` | PowerOutage.us historical outage curves |
| 9 | **Cause taxonomy and trends** | Cause categories in marts and `/reliability` context | dbt macro `classify_cause`, `marts.outage_cause_daily` | Energex shows “likely cause”; DNO fault analytics |
| 10 | **Pipeline observability** | Status chips (Kafka / Flink / Spark) and `/api/health` | `ops.pipeline_heartbeat`, direct Kafka TCP + Flink REST probes | SRE practice for data products |
| 11 | **Outage hotspot clustering** | `ml.outage_clusters` (batch, ready for a map layer) | Spark MLlib KMeans on lat/lon/hour | Distribution-network hotspot analysis |
| 12 | **Data lake archive** | Daily Parquet under `data/lake/outages/dt=…` | Spark daily batch | Standard utility/analytics lake pattern |

## Ideas evaluated but intentionally not built

- **Service-territory polygons / adjacency view** (PowerOutage.us): the current
  district polygons are already shown; a utility-territory overlay would need
  DISCOM boundary data that is not published as open GeoJSON.
- **Address/postcode search** (UKPN/Energex): India does not have a comparable
  postcode→feeder mapping here; district + substation search is the practical
  equivalent and already exists in the details table.
- **Priority-services register** (UKPN): requires personal data and a consent
  flow; out of scope for an anonymous public dashboard.
- **SMS/email restoration notifications**: needs an outbound provider and user
  accounts; the live alert feed is the read-only equivalent for now.

## Why these “foreign” features fit Odisha

- Odisha’s DISCOM feed is **planned-outage heavy**; crowd reports add the
  unplanned/real-time signal that PowerOutage.us gets from utility telemetry.
- Monsoon-driven faults make **weather risk** and **restoration estimates** more
  useful than static outage counts.
- Regulatory-style **SAIDI/SAIFI** give a comparable reliability picture across
  districts, which the raw feed cannot express.

## Reference links

- PowerOutage.us — real-time outage tracking and data API:
  <https://poweroutage.us/use-our-data/emergency-management-departments>
- UK Power Networks — live power cut list with restoration estimates:
  <https://www.ukpowernetworks.co.uk/power-cut/list>
- Energex — outage finder (current/planned, suburbs, cause, ETR):
  <https://www.energex.com.au/outages/outage-finder/>
- IEEE 1366 reliability indices (SAIDI/SAIFI/CAIDI) overview, as used in US
  regulatory reporting:
  <https://www.puc.pa.gov/media/4049/2025-electric-reliability-report-final_sep_2026.pdf>
