package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type cacheItem struct {
	body []byte
	at   time.Time
}

func (s *AppState) cached(key string, ttl time.Duration) []byte {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	item, ok := s.cache[key]
	if !ok || time.Since(item.at) > ttl {
		return nil
	}
	return item.body
}

func (s *AppState) setCache(key string, body []byte) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	s.cache[key] = cacheItem{body: body, at: time.Now()}
}

func (s *AppState) ensureStore() *Store {
	s.dbMu.Lock()
	defer s.dbMu.Unlock()
	if s.store != nil {
		return s.store
	}
	if time.Since(s.lastDBTry) < 10*time.Second && !s.lastDBTry.IsZero() {
		return nil
	}
	s.lastDBTry = time.Now()
	st, err := OpenStore(s.cfg.DatabaseURL)
	if err != nil {
		log.Printf("postgres not ready: %v", err)
		return nil
	}
	log.Printf("connected to postgres")
	s.store = st
	return st
}

func (s *AppState) dropStore() {
	s.dbMu.Lock()
	defer s.dbMu.Unlock()
	if s.store != nil {
		s.store.Close()
		s.store = nil
	}
}

func isConnErr(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	for _, needle := range []string{"connection refused", "connection reset", "broken pipe",
		"no such host", "server closed the connection", "database system is starting up",
		"terminating connection", "i/o timeout"} {
		if strings.Contains(msg, needle) {
			return true
		}
	}
	return false
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func (s *AppState) cors(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next(w, r)
	}
}

func (s *AppState) loadSnapshot() (*MapResponse, error) {
	for _, p := range s.cfg.SnapshotPaths {
		if p == "" {
			continue
		}
		data, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var m MapResponse
		if err := json.Unmarshal(data, &m); err != nil {
			log.Printf("bad snapshot %s: %v", p, err)
			continue
		}
		if m.Dots == nil {
			m.Dots = []Dot{}
		}
		if m.Districts == nil {
			m.Districts = []DistrictData{}
		}
		m.Source = "snapshot"
		return &m, nil
	}
	return nil, fmt.Errorf("no snapshot available")
}

// ---------------------------------------------------------------------------
// GET /api/mapdata
// ---------------------------------------------------------------------------

func (s *AppState) handleMapData(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if body := s.cached("mapdata", 20*time.Second); body != nil && !s.cfg.SnapshotOnly {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}

	if !s.cfg.SnapshotOnly {
		if st := s.ensureStore(); st != nil {
			ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
			m, err := st.MapData(ctx)
			cancel()
			if err == nil {
				if body, mErr := json.Marshal(m); mErr == nil {
					s.setCache("mapdata", body)
					w.Header().Set("Content-Type", "application/json; charset=utf-8")
					w.WriteHeader(http.StatusOK)
					_, _ = w.Write(body)
					return
				}
			}
			log.Printf("mapdata from postgres failed: %v", err)
			if isConnErr(err) {
				s.dropStore()
			}
		}

	}
	m, err := s.loadSnapshot()
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "no datamart available yet",
		})
		return
	}
	if body, mErr := json.Marshal(m); mErr == nil {
		s.setCache("mapdata", body)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}
	writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "encode failed"})
}

// ---------------------------------------------------------------------------
// GET /api/realtime
// ---------------------------------------------------------------------------

func (s *AppState) handleRealtime(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if body := s.cached("realtime", 5*time.Second); body != nil {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}

	empty := &RealtimeResponse{
		Available:     false,
		WindowMinutes: 15,
		Districts:     []RealtimeDistrict{},
		Alerts:        []Alert{},
		Trend:         []TrendPoint{},
		Message:       "realtime store unavailable",
	}
	st := s.ensureStore()
	if st == nil {
		writeJSON(w, http.StatusOK, empty)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	resp, err := st.Realtime(ctx)
	if err != nil {
		cancel()
		log.Printf("realtime query failed: %v", err)
		if isConnErr(err) {
			s.dropStore()
		}
		writeJSON(w, http.StatusOK, empty)
		return
	}
	resp.Pipeline = s.pipelineStatus(ctx, st)
	cancel()

	body, err := json.Marshal(resp)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "encode failed"})
		return
	}
	s.setCache("realtime", body)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// pipelineStatus probes Kafka, Flink, Spark and Airflow with short timeouts.
func (s *AppState) pipelineStatus(parent context.Context, st *Store) PipelineStatus {
	ctx, cancel := context.WithTimeout(parent, 1500*time.Millisecond)
	defer cancel()

	status := PipelineStatus{Kafka: "unknown", Flink: "unknown", Spark: "unknown", Airflow: "unknown"}
	var wg sync.WaitGroup

	wg.Add(1)
	go func() {
		defer wg.Done()
		conn, err := (&net.Dialer{Timeout: 800 * time.Millisecond}).DialContext(ctx, "tcp", s.cfg.KafkaBroker)
		if err == nil {
			status.Kafka = "ok"
			_ = conn.Close()
			return
		}
		status.Kafka = "down"
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		out, err := httpGetJSON(ctx, s.cfg.FlinkURL+"/overview")
		if err != nil {
			status.Flink = "down"
			return
		}
		if n, ok := out["taskmanagers"].(float64); ok && n > 0 {
			status.Flink = "ok"
		} else {
			status.Flink = "degraded"
		}
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		hbs, err := st.Heartbeats(ctx)
		if err != nil {
			status.Spark = "unknown"
			return
		}
		if h, ok := hbs["spark-stream"]; ok {
			status.LastSparkUpdate = h.LastSeen.UTC().Format(time.RFC3339)
			if time.Since(h.LastSeen) < 3*time.Minute {
				status.Spark = "ok"
			} else {
				status.Spark = "stale"
			}
		} else {
			status.Spark = "down"
		}
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		if s.cfg.AirflowURL == "" {
			status.Airflow = "unknown"
			return
		}
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.AirflowURL+"/health", nil)
		if s.cfg.AirflowUser != "" {
			req.SetBasicAuth(s.cfg.AirflowUser, s.cfg.AirflowPass)
		}
		client := http.Client{Timeout: 900 * time.Millisecond}
		res, err := client.Do(req)
		if err != nil {
			status.Airflow = "down"
			return
		}
		_ = res.Body.Close()
		if res.StatusCode == http.StatusOK {
			status.Airflow = "ok"
		} else {
			status.Airflow = "down"
		}
	}()

	wg.Wait()
	return status
}

func httpGetJSON(ctx context.Context, url string) (map[string]any, error) {
	if url == "" {
		return nil, fmt.Errorf("no url")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	client := http.Client{Timeout: 900 * time.Millisecond}
	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d", res.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// GET /api/reliability
// ---------------------------------------------------------------------------

func (s *AppState) handleReliability(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	days := 30
	if v := r.URL.Query().Get("days"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			days = n
		}
	}
	if days < 1 {
		days = 1
	}
	if days > 365 {
		days = 365
	}
	key := fmt.Sprintf("reliability:%d", days)
	if body := s.cached(key, 60*time.Second); body != nil {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}

	empty := &ReliabilityResponse{Available: false, Days: days,
		Districts: []ReliabilityDistrict{}, Trend: []ReliabilityTrend{},
		Message: "reliability marts not built yet"}
	st := s.ensureStore()
	if st == nil {
		writeJSON(w, http.StatusOK, empty)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 12*time.Second)
	defer cancel()
	resp, err := st.Reliability(ctx, days)
	if err != nil {
		log.Printf("reliability query failed: %v", err)
		if isConnErr(err) {
			s.dropStore()
		}
		writeJSON(w, http.StatusOK, empty)
		return
	}
	body, err := json.Marshal(resp)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "encode failed"})
		return
	}
	s.setCache(key, body)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// ---------------------------------------------------------------------------
// GET /api/risk
// ---------------------------------------------------------------------------

func (s *AppState) handleRisk(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if body := s.cached("risk", 60*time.Second); body != nil {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
		return
	}
	empty := &RiskResponse{Available: false, Districts: []RiskDistrict{},
		Message: "risk marts not built yet"}
	st := s.ensureStore()
	if st == nil {
		writeJSON(w, http.StatusOK, empty)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := st.Risk(ctx)
	if err != nil {
		log.Printf("risk query failed: %v", err)
		if isConnErr(err) {
			s.dropStore()
		}
		writeJSON(w, http.StatusOK, empty)
		return
	}
	body, err := json.Marshal(resp)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "encode failed"})
		return
	}
	s.setCache("risk", body)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// ---------------------------------------------------------------------------
// GET /api/map-reports and POST /api/report
// ---------------------------------------------------------------------------

func (s *AppState) handleMapReports(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	st := s.ensureStore()
	if st == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "community reports unavailable"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	reports, err := st.MapReports(ctx)
	if err != nil {
		log.Printf("map reports: %v", err)
		if isConnErr(err) {
			s.dropStore()
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "community reports unavailable"})
		return
	}
	writeJSON(w, http.StatusOK, reports)
}

func (s *AppState) handleReport(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var in ReportInput
	body, err := io.ReadAll(io.LimitReader(r.Body, 32<<10))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot read body"})
		return
	}
	if err := json.Unmarshal(body, &in); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if in.Kind != "" && in.Kind != "outage" && in.Kind != "correction" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid report kind"})
		return
	}
	in.District = strings.TrimSpace(in.District)
	in.GSS = strings.TrimSpace(in.GSS)
	in.Feeder = strings.TrimSpace(in.Feeder)
	in.Comment = strings.TrimSpace(in.Comment)
	if in.District == "" || len(in.District) > 80 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "district is required"})
		return
	}
	if len(in.Comment) > 1000 {
		in.Comment = in.Comment[:1000]
	}
	if in.Severity < 1 || in.Severity > 5 {
		in.Severity = 3
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	reportID := newReportID()
	queued := false
	if s.producer != nil && in.Kind != "correction" {
		if err := s.producer.PublishReport(ctx, in, reportID); err != nil {
			log.Printf("kafka publish failed: %v", err)
		} else {
			queued = true
		}
	}
	stored := false
	if st := s.ensureStore(); st != nil {
		if err := st.InsertReport(ctx, in, reportID); err != nil {
			log.Printf("report insert failed: %v", err)
			if isConnErr(err) {
				s.dropStore()
			}
		} else {
			stored = true
		}
	}
	if !queued && !stored {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "report pipeline unavailable",
		})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"status":   "ok",
		"reportId": reportID,
		"queued":   queued,
		"stored":   stored,
	})
}

// ---------------------------------------------------------------------------
// POST /api/refresh — trigger the Airflow official-ingest DAG
// ---------------------------------------------------------------------------

func (s *AppState) handleRefresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.AirflowURL == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "AIRFLOW_URL not configured",
		})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()
	payload := fmt.Sprintf(`{"dag_run_id":"manual__%s","conf":{}}`,
		time.Now().UTC().Format("2006-01-02T15:04:05"))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		s.cfg.AirflowURL+"/api/v1/dags/odisha_official_ingest/dagRuns",
		strings.NewReader(payload))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if s.cfg.AirflowUser != "" {
		req.SetBasicAuth(s.cfg.AirflowUser, s.cfg.AirflowPass)
	}
	client := http.Client{Timeout: 7 * time.Second}
	res, err := client.Do(req)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	defer res.Body.Close()
	if res.StatusCode >= 300 {
		detail, _ := io.ReadAll(io.LimitReader(res.Body, 2048))
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error":  "airflow rejected the run",
			"detail": string(detail),
		})
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "refreshing"})
}

// ---------------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------------

func (s *AppState) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	components := map[string]HealthComponent{}
	overall := "ok"

	if s.cfg.SnapshotOnly {
		// Check readability without decoding the feed or probing optional services.
		for _, path := range s.cfg.SnapshotPaths {
			if f, err := os.Open(path); err == nil {
				f.Close()
				writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "mode": "snapshot"})
				return
			}
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "missing snapshot", "mode": "snapshot"})
		return
	}
	// Postgres
	start := time.Now()
	st := s.ensureStore()
	if st == nil {
		components["postgres"] = HealthComponent{Status: "down", LatencyMS: time.Since(start).Milliseconds()}
		overall = "degraded"
	} else {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		err := st.Ping(ctx)
		cancel()
		if err != nil {
			components["postgres"] = HealthComponent{Status: "down", Detail: err.Error()}
			overall = "degraded"
		} else {
			components["postgres"] = HealthComponent{Status: "ok", LatencyMS: time.Since(start).Milliseconds()}
		}
	}

	// Kafka (TCP)
	start = time.Now()
	conn, err := net.DialTimeout("tcp", s.cfg.KafkaBroker, 1200*time.Millisecond)
	if err != nil {
		components["kafka"] = HealthComponent{Status: "down", Detail: err.Error()}
		overall = "degraded"
	} else {
		_ = conn.Close()
		components["kafka"] = HealthComponent{Status: "ok", LatencyMS: time.Since(start).Milliseconds()}
	}

	// Flink REST
	start = time.Now()
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	flink, flinkErr := httpGetJSON(ctx, s.cfg.FlinkURL+"/overview")
	cancel()
	if flinkErr != nil {
		components["flink"] = HealthComponent{Status: "down", Detail: flinkErr.Error()}
		overall = "degraded"
	} else {
		detail := ""
		if v, ok := flink["taskmanagers"].(float64); ok {
			detail = fmt.Sprintf("%.0f taskmanager(s)", v)
		}
		components["flink"] = HealthComponent{Status: "ok", LatencyMS: time.Since(start).Milliseconds(), Detail: detail}
	}

	// Spark heartbeat
	if st != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		hbs, _ := st.Heartbeats(ctx)
		cancel()
		if h, ok := hbs["spark-stream"]; ok {
			status := "ok"
			if time.Since(h.LastSeen) > 3*time.Minute {
				status = "stale"
			}
			components["spark"] = HealthComponent{Status: status, Detail: h.LastSeen.UTC().Format(time.RFC3339)}
		} else {
			components["spark"] = HealthComponent{Status: "down"}
		}
	}

	writeJSON(w, http.StatusOK, HealthResponse{
		Status:     overall,
		Version:    appVersion,
		UptimeSec:  int64(time.Since(s.started).Seconds()),
		Components: components,
	})
}

// GET observes a refresh; only an explicit POST can start one. The private
// worker owns the Postgres lock and continues if a browser disconnects.
func (s *AppState) handleSnapshotRefresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 7*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, r.Method, "http://refresh:8091/refresh", nil)
	if err != nil {
		writeJSON(w, 500, map[string]string{"error": "refresh request failed"})
		return
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		writeJSON(w, 503, map[string]string{"error": "refresh service unavailable"})
		return
	}
	defer res.Body.Close()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(res.StatusCode)
	_, _ = io.Copy(w, io.LimitReader(res.Body, 4096))
}
