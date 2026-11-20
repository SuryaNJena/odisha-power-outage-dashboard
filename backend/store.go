package main

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

// Store wraps the analytics/Postgres database. A nil *Store means "database
// not configured or not reachable"; callers must degrade gracefully.
type Store struct {
	db *sql.DB
}

func OpenStore(dsn string) (*Store, error) {
	if strings.TrimSpace(dsn) == "" {
		return nil, fmt.Errorf("empty DATABASE_URL")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(6)
	db.SetMaxIdleConns(3)
	db.SetConnMaxIdleTime(2 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() {
	if s != nil && s.db != nil {
		_ = s.db.Close()
	}
}

func (s *Store) Ping(ctx context.Context) error {
	if s == nil {
		return fmt.Errorf("no database")
	}
	return s.db.PingContext(ctx)
}

func fmtTime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}

func fmtTimePtr(t sql.NullTime) string {
	if !t.Valid {
		return ""
	}
	return fmtTime(t.Time)
}

// ---------------------------------------------------------------------------
// Map data (planning datamart)
// ---------------------------------------------------------------------------

func (s *Store) MapData(ctx context.Context) (*MapResponse, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT district, COALESCE(circle,''), COALESCE(division,''), COALESCE(gss,''),
		       COALESCE(voltage,''), COALESCE(feeder,''),
		       COALESCE(start_text,''), COALESCE(end_text,''),
		       COALESCE(outage_date, CURRENT_DATE)::text,
		       COALESCE(reason,''), COALESCE(areas,''),
		       COALESCE(lat, 0), COALESCE(lon, 0), COALESCE(geocoded, false),
		       COALESCE(is_planned, false), COALESCE(duration_minutes, 0),
		       COALESCE(affected_customers, 0),
		       COALESCE(cause_category,''), etr
		FROM marts.map_dots`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	resp := &MapResponse{Dots: []Dot{}, Districts: []DistrictData{}, Source: "postgres"}
	counts := map[string]int{}
	for rows.Next() {
		var (
			d         Dot
			date      string
			planned   bool
			duration  float64
			customers int
			cause     string
			etr       sql.NullTime
		)
		if err := rows.Scan(
			&d.District, &d.Circle, &d.Division, &d.GSS,
			&d.Voltage, &d.Feeder, &d.Start, &d.End,
			&date, &d.Reason, &d.Areas,
			&d.Lat, &d.Lon, &d.Geocoded,
			&planned, &duration, &customers, &cause, &etr,
		); err != nil {
			return nil, err
		}
		d.Date = date
		d.IsPlanned = &planned
		d.DurationMinutes = &duration
		d.AffectedCustomers = &customers
		if cause != "" {
			d.Cause = &cause
		}
		if v := fmtTimePtr(etr); v != "" {
			d.ETR = &v
		}
		if d.District == "" {
			continue
		}
		resp.Dots = append(resp.Dots, d)
		counts[d.District]++
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	for name, n := range counts {
		resp.Districts = append(resp.Districts, DistrictData{Name: name, Value: n})
	}
	sort.Slice(resp.Districts, func(i, j int) bool {
		if resp.Districts[i].Value == resp.Districts[j].Value {
			return resp.Districts[i].Name < resp.Districts[j].Name
		}
		return resp.Districts[i].Value > resp.Districts[j].Value
	})
	resp.Total = len(resp.Dots)
	resp.UpdatedAt = s.lastBatchTime(ctx)
	return resp, nil
}

func (s *Store) lastBatchTime(ctx context.Context) string {
	var t sql.NullTime
	err := s.db.QueryRowContext(ctx, `
		SELECT COALESCE(max(finished_at), max(started_at))
		FROM raw.scrape_batches WHERE status = 'success'`).Scan(&t)
	if err != nil || !t.Valid {
		return fmtTime(time.Now())
	}
	return fmtTime(t.Time)
}

// ---------------------------------------------------------------------------
// Realtime (NRT) metrics
// ---------------------------------------------------------------------------

func riskLevel(score float64) string {
	switch {
	case score >= 0.75:
		return "severe"
	case score >= 0.5:
		return "high"
	case score >= 0.25:
		return "moderate"
	default:
		return "low"
	}
}

func (s *Store) Realtime(ctx context.Context) (*RealtimeResponse, error) {
	resp := &RealtimeResponse{
		Available:     true,
		WindowMinutes: 15,
		Districts:     []RealtimeDistrict{},
		Alerts:        []Alert{},
		Trend:         []TrendPoint{},
	}

	rows, err := s.db.QueryContext(ctx, `
		SELECT district, report_count, unique_reporters, official_outages,
		       unconfirmed_reports, COALESCE(risk_score, 0),
		       window_start, window_end, updated_at
		FROM realtime.district_live_metrics
		ORDER BY report_count DESC, district`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	latest := time.Time{}
	maxUpdated := time.Time{}
	for rows.Next() {
		var (
			d         RealtimeDistrict
			score     float64
			ws, we, u sql.NullTime
		)
		if err := rows.Scan(&d.District, &d.Reports, &d.UniqueReporters,
			&d.OfficialOutages, &d.UnconfirmedReports, &score,
			&ws, &we, &u); err != nil {
			return nil, err
		}
		d.RiskScore = score
		d.RiskLevel = riskLevel(score)
		d.UpdatedAt = fmtTimePtr(u)
		if u.Valid && u.Time.After(maxUpdated) {
			maxUpdated = u.Time
		}
		if we.Valid && we.Time.After(latest) {
			latest = we.Time
		}
		resp.Districts = append(resp.Districts, d)
		resp.Totals.Reports += d.Reports
		resp.Totals.OfficialOutages += d.OfficialOutages
		if d.Reports > 0 {
			resp.Totals.Districts++
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Latest alerts.
	arows, err := s.db.QueryContext(ctx, `
		SELECT alert_id, COALESCE(district,''), alert_type, severity, message,
		       COALESCE(report_count,0), window_start, window_end, created_at
		FROM realtime.alerts
		ORDER BY created_at DESC LIMIT 20`)
	if err != nil {
		return nil, err
	}
	for arows.Next() {
		var (
			a          Alert
			ws, we, ca sql.NullTime
		)
		if err := arows.Scan(&a.AlertID, &a.District, &a.AlertType, &a.Severity,
			&a.Message, &a.ReportCount, &ws, &we, &ca); err != nil {
			arows.Close()
			return nil, err
		}
		a.WindowStart = fmtTimePtr(ws)
		a.WindowEnd = fmtTimePtr(we)
		a.CreatedAt = fmtTimePtr(ca)
		resp.Alerts = append(resp.Alerts, a)
	}
	arows.Close()
	resp.Totals.Alerts = len(resp.Alerts)

	// Reports-per-minute trend, last hour.
	trows, err := s.db.QueryContext(ctx, `
		SELECT date_trunc('minute', bucket), SUM(report_count)::int
		FROM realtime.report_minute
		WHERE bucket > now() - interval '60 minutes'
		GROUP BY 1 ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	for trows.Next() {
		var (
			b time.Time
			n int
		)
		if err := trows.Scan(&b, &n); err != nil {
			trows.Close()
			return nil, err
		}
		resp.Trend = append(resp.Trend, TrendPoint{Bucket: fmtTime(b), Reports: n})
	}
	trows.Close()

	if maxUpdated.IsZero() {
		maxUpdated = latest
	}
	if maxUpdated.IsZero() {
		maxUpdated = time.Now()
	}
	resp.UpdatedAt = fmtTime(maxUpdated)
	return resp, nil
}

// ---------------------------------------------------------------------------
// Reliability indices
// ---------------------------------------------------------------------------

func (s *Store) Reliability(ctx context.Context, days int) (*ReliabilityResponse, error) {
	if days <= 0 || days > 365 {
		days = 30
	}
	resp := &ReliabilityResponse{Available: true, Days: days,
		Districts: []ReliabilityDistrict{}, Trend: []ReliabilityTrend{}}

	rows, err := s.db.QueryContext(ctx, `
		SELECT district,
		       COALESCE(SUM(saidi_minutes),0)::float8,
		       COALESCE(SUM(saifi),0)::float8,
		       CASE WHEN SUM(saifi) > 0 THEN SUM(saidi_minutes)/SUM(saifi) ELSE 0 END::float8,
		       COALESCE(AVG(asai_pct),100)::float8,
		       COALESCE(SUM(outage_count),0)::int,
		       COALESCE(SUM(customers_affected),0)::bigint
		FROM analytics.reliability_daily
		WHERE metric_date >= CURRENT_DATE - $1::int
		GROUP BY district
		ORDER BY 2 DESC`, days)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var d ReliabilityDistrict
		if err := rows.Scan(&d.District, &d.SAIDIMinutes, &d.SAIFI, &d.CAIDIMinutes,
			&d.ASAIPct, &d.OutageCount, &d.CustomersAffected); err != nil {
			return nil, err
		}
		resp.Districts = append(resp.Districts, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	trows, err := s.db.QueryContext(ctx, `
		SELECT metric_date, COALESCE(SUM(saidi_minutes),0)::float8,
		       COALESCE(SUM(saifi),0)::float8
		FROM analytics.reliability_daily
		WHERE metric_date >= CURRENT_DATE - $1::int
		GROUP BY 1 ORDER BY 1`, days)
	if err != nil {
		return nil, err
	}
	for trows.Next() {
		var t ReliabilityTrend
		var d time.Time
		if err := trows.Scan(&d, &t.SAIDIMinutes, &t.SAIFI); err != nil {
			trows.Close()
			return nil, err
		}
		t.Date = d.Format("2006-01-02")
		resp.Trend = append(resp.Trend, t)
	}
	trows.Close()
	return resp, nil
}

// ---------------------------------------------------------------------------
// Weather-driven risk
// ---------------------------------------------------------------------------

func (s *Store) Risk(ctx context.Context) (*RiskResponse, error) {
	// Prefer the Spark ML forecast (next 24h) when the daily batch has run.
	if resp, err := s.riskFromML(ctx); err == nil && len(resp.Districts) > 0 {
		return resp, nil
	}
	// Fall back to the dbt weather-risk marts.
	resp := &RiskResponse{Available: true, Districts: []RiskDistrict{}}
	rows, err := s.db.QueryContext(ctx, `
		SELECT district, COALESCE(risk_score,0)::float8, COALESCE(risk_level,'low'),
		       COALESCE(expected_outages,0), COALESCE(max_precip_mm,0)::float8,
		       COALESCE(max_wind_kph,0)::float8, risk_date
		FROM analytics.district_risk_daily
		WHERE risk_date = (SELECT max(risk_date) FROM analytics.district_risk_daily)
		ORDER BY 2 DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var updated time.Time
	for rows.Next() {
		var d RiskDistrict
		var date time.Time
		if err := rows.Scan(&d.District, &d.RiskScore, &d.RiskLevel,
			&d.ExpectedOutages, &d.MaxPrecipMM, &d.MaxWindKPH, &date); err != nil {
			return nil, err
		}
		d.ForecastDate = date.Format("2006-01-02")
		if date.After(updated) {
			updated = date
		}
		resp.Districts = append(resp.Districts, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	resp.UpdatedAt = fmtTime(updated)
	return resp, nil
}

// riskFromML reads the Spark daily-batch forecast. It returns an error when the
// ml table is missing or empty so Risk() can fall back to the dbt marts.
func (s *Store) riskFromML(ctx context.Context) (*RiskResponse, error) {
	resp := &RiskResponse{Available: true, Districts: []RiskDistrict{}}
	rows, err := s.db.QueryContext(ctx, `
		SELECT district, COALESCE(risk_score,0)::float8, COALESCE(risk_level,'low'),
		       COALESCE(expected_outages,0)::float8,
		       COALESCE(max_precip_mm,0)::float8, COALESCE(max_wind_kph,0)::float8,
		       forecast_date
		FROM ml.district_risk_forecast
		WHERE forecast_date = (SELECT max(forecast_date) FROM ml.district_risk_forecast)
		ORDER BY 2 DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var updated time.Time
	for rows.Next() {
		var d RiskDistrict
		var date time.Time
		if err := rows.Scan(&d.District, &d.RiskScore, &d.RiskLevel,
			&d.ExpectedOutages, &d.MaxPrecipMM, &d.MaxWindKPH, &date); err != nil {
			return nil, err
		}
		d.ForecastDate = date.Format("2006-01-02")
		if date.After(updated) {
			updated = date
		}
		resp.Districts = append(resp.Districts, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	resp.UpdatedAt = fmtTime(updated)
	return resp, nil
}

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

func (s *Store) MapReports(ctx context.Context) ([]MapReport, error) {
	rows, err := s.db.QueryContext(ctx, `
        WITH recent AS (
            SELECT district, regexp_replace(lower(district), '[^a-z0-9]+', '', 'g') AS district_key,
                   count(*) AS reports, max(reported_at) AS latest_at
            FROM raw.user_reports
            WHERE reported_at >= now() - interval '24 hours'
              AND (source = 'web-outage' OR (
                  source = 'web' AND coalesce(trim(gss), '') = '' AND coalesce(trim(feeder), '') = ''
              ))
            GROUP BY district
        )
        SELECT c.district, c.lat, c.lon, sum(r.reports)::bigint, max(r.latest_at)
        FROM recent r
        LEFT JOIN raw.district_aliases a ON a.alias = r.district_key
        JOIN raw.district_centroids c
          ON c.district = a.district OR (a.district IS NULL AND lower(c.district) = lower(r.district))
        WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL
        GROUP BY c.district, c.lat, c.lon
        ORDER BY c.district`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	reports := make([]MapReport, 0)
	for rows.Next() {
		var report MapReport
		if err := rows.Scan(&report.District, &report.Lat, &report.Lon, &report.Count, &report.LatestAt); err != nil {
			return nil, err
		}
		reports = append(reports, report)
	}
	return reports, rows.Err()
}

func (s *Store) InsertReport(ctx context.Context, r ReportInput, reportID string) error {
	source := "web"
	switch r.Kind {
	case "outage":
		source = "web-outage"
	case "correction":
		source = "web-correction"
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO raw.user_reports
			(report_id, district, gss, feeder, comment, severity, reporter,
			 lat, lon, reported_at, source)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),$10)
		ON CONFLICT (report_id) DO NOTHING`,
		reportID, r.District, r.GSS, r.Feeder, r.Comment, r.Severity, "web", r.Lat, r.Lon, source)
	return err
}

// ---------------------------------------------------------------------------
// Heartbeats
// ---------------------------------------------------------------------------

type heartbeat struct {
	Component string
	Status    string
	LastSeen  time.Time
}

func (s *Store) Heartbeats(ctx context.Context) (map[string]heartbeat, error) {
	out := map[string]heartbeat{}
	rows, err := s.db.QueryContext(ctx, `SELECT component, status, last_seen FROM ops.pipeline_heartbeat`)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var h heartbeat
		if err := rows.Scan(&h.Component, &h.Status, &h.LastSeen); err != nil {
			return out, err
		}
		out[h.Component] = h
	}
	return out, rows.Err()
}
