package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

const appVersion = "2.0.0-platform"

type Config struct {
	SnapshotOnly  bool
	Port          string
	DatabaseURL   string
	KafkaBroker   string
	KafkaTopic    string
	DataDir       string
	WebDir        string
	AirflowURL    string
	AirflowUser   string
	AirflowPass   string
	FlinkURL      string
	SnapshotPaths []string
}

type AppState struct {
	cfg      Config
	started  time.Time
	store    *Store
	producer *Producer

	dbMu      sync.Mutex
	lastDBTry time.Time

	cacheMu sync.Mutex
	cache   map[string]cacheItem
}

func env(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func loadConfig() Config {
	cfg := Config{
		SnapshotOnly: env("ODISHA_MODE", "snapshot") != "full",
		Port:         env("PORT", "3001"),
		DatabaseURL:  env("DATABASE_URL", ""),
		KafkaBroker:  env("KAFKA_BROKERS", env("KAFKA_BOOTSTRAP", "kafka:9092")),
		KafkaTopic:   env("KAFKA_TOPIC_REPORTS", "user-reports"),
		DataDir:      env("DATA_DIR", "/opt/odisha-data"),
		WebDir:       env("WEB_DIR", "/app/out"),
		AirflowURL:   strings.TrimRight(env("AIRFLOW_URL", ""), "/"),
		AirflowUser:  env("AIRFLOW_USER", ""),
		AirflowPass:  env("AIRFLOW_PASSWORD", ""),
		FlinkURL:     strings.TrimRight(env("FLINK_URL", "http://flink-jobmanager:8081"), "/"),
	}
	if cfg.DatabaseURL == "" {
		log.Printf("WARNING: DATABASE_URL is empty — serving only the snapshot")
	}
	seen := map[string]bool{}
	add := func(p string) {
		if p == "" || seen[p] {
			return
		}
		seen[p] = true
		cfg.SnapshotPaths = append(cfg.SnapshotPaths, p)
	}
	add(filepath.Join(cfg.DataDir, "mart", "outages.json"))
	add("/app/data/mart/outages.json")
	add(filepath.Join("..", "data", "mart", "outages.json"))
	add(filepath.Join("data", "mart", "outages.json"))
	return cfg
}

// staticHandler serves the Next.js static export, including pretty URLs such
// as /live -> live.html, which is how the app router export is laid out.
func (s *AppState) staticHandler() http.HandlerFunc {
	fs := http.FileServer(http.Dir(s.cfg.WebDir))
	return func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api") {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		p := path.Clean("/" + r.URL.Path)
		if strings.Contains(p, "..") {
			http.NotFound(w, r)
			return
		}
		if p == "/" {
			p = "/index.html"
		}
		full := filepath.Join(s.cfg.WebDir, filepath.FromSlash(p))
		if fi, err := os.Stat(full); err == nil && !fi.IsDir() {
			fs.ServeHTTP(w, r)
			return
		}
		if !strings.HasSuffix(p, ".html") {
			if fi, err := os.Stat(full + ".html"); err == nil && !fi.IsDir() {
				http.ServeFile(w, r, full+".html")
				return
			}
		}
		if fi, err := os.Stat(full); err == nil && fi.IsDir() {
			index := filepath.Join(full, "index.html")
			if _, err := os.Stat(index); err == nil {
				http.ServeFile(w, r, index)
				return
			}
		}
		if _, err := os.Stat(filepath.Join(s.cfg.WebDir, "404.html")); err == nil {
			w.WriteHeader(http.StatusNotFound)
			http.ServeFile(w, r, filepath.Join(s.cfg.WebDir, "404.html"))
			return
		}
		http.NotFound(w, r)
	}
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	cfg := loadConfig()

	state := &AppState{
		cfg:     cfg,
		started: time.Now(),
		cache:   map[string]cacheItem{},
	}

	// The database may still be initialising; retry lazily from handlers.
	if !cfg.SnapshotOnly {
		if st, err := OpenStore(cfg.DatabaseURL); err == nil {
			state.store = st
			log.Printf("postgres connected")
		} else {
			log.Printf("postgres unavailable at startup: %v", err)
		}
		state.producer = NewProducer(cfg.KafkaBroker, cfg.KafkaTopic)
		if state.producer != nil {
			log.Printf("kafka producer enabled: %s topic=%s", cfg.KafkaBroker, cfg.KafkaTopic)
		}
	}
	defer state.producer.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/api/mapdata", state.cors(state.handleMapData))
	mux.HandleFunc("/api/features", state.cors(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"analytics": !cfg.SnapshotOnly, "reporting": true, "manualRefresh": cfg.SnapshotOnly})
	}))
	if cfg.SnapshotOnly {
		mux.HandleFunc("/api/refresh", state.cors(state.handleSnapshotRefresh))
		mux.HandleFunc("/api/report", state.cors(state.handleReport))
		for _, route := range []string{"/api/realtime", "/api/reliability", "/api/risk"} {
			mux.HandleFunc(route, state.cors(func(w http.ResponseWriter, r *http.Request) {
				writeJSON(w, http.StatusServiceUnavailable, map[string]any{"available": false, "mode": "snapshot", "error": "Disabled in manual snapshot mode"})
			}))
		}
	} else {
		mux.HandleFunc("/api/realtime", state.cors(state.handleRealtime))
		mux.HandleFunc("/api/reliability", state.cors(state.handleReliability))
		mux.HandleFunc("/api/risk", state.cors(state.handleRisk))
		mux.HandleFunc("/api/report", state.cors(state.handleReport))
		mux.HandleFunc("/api/refresh", state.cors(state.handleRefresh))
	}
	mux.HandleFunc("/api/health", state.cors(state.handleHealth))
	mux.HandleFunc("/", state.cors(state.staticHandler()))

	srv := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		log.Printf("listening on :%s (web=%s data=%s)", cfg.Port, cfg.WebDir, cfg.DataDir)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	state.dbMu.Lock()
	if state.store != nil {
		state.store.Close()
	}
	state.dbMu.Unlock()
	log.Printf("bye")
}
