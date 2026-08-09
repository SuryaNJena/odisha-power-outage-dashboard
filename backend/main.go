package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type DistrictData struct {
	Name  string `json:"name"`
	Value int    `json:"value"`
}

type Dot struct {
	District string  `json:"district"`
	Circle   string  `json:"circle"`
	Division string  `json:"division"`
	GSS      string  `json:"gss"`
	Voltage  string  `json:"voltage"`
	Feeder   string  `json:"feeder"`
	Start    string  `json:"start"`
	End      string  `json:"end"`
	Reason   string  `json:"reason"`
	Areas    string  `json:"areas"`
	Lat      float64 `json:"lat"`
	Lon      float64 `json:"lon"`
	Geocoded bool    `json:"geocoded"`
}

type MapResponse struct {
	Districts []DistrictData `json:"districts"`
	Dots      []Dot          `json:"dots"`
	Total     int            `json:"total"`
	UpdatedAt string         `json:"updatedAt"`
	Source    string         `json:"source"`
}

type Report struct {
	District   string `json:"district"`
	GSS        string `json:"gss"`
	Feeder     string `json:"feeder"`
	Comment    string `json:"comment"`
	ReportedAt string `json:"reportedAt"`
}

type AppState struct {
	mu              sync.RWMutex
	mart            *MapResponse
	root            string
	martPath        string
	reportPath      string
	pipelineRunning bool
	lastError       string
}

func resolveRoot() string {
	if _, err := os.Stat(filepath.Join(".", "data")); err == nil {
		return "."
	}
	return ".."
}

func loadMart(path string) (*MapResponse, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m MapResponse
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

func (s *AppState) getData() *MapResponse {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.mart == nil {
		return &MapResponse{Districts: []DistrictData{}, Dots: []Dot{}, Total: 0, UpdatedAt: "", Source: "empty"}
	}
	return s.mart
}

// runPipeline executes scrap.py then process.py from the repo root.
func (s *AppState) runPipeline() error {
	for _, script := range []string{"scrap.py", "process.py"} {
		cmd := exec.Command("python", script)
		cmd.Dir = s.root
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("pipeline step %s failed: %w\n%s", script, err, string(out))
		}
		log.Printf("pipeline %s ok: %s", script, lastLines(string(out), 3))
	}
	return nil
}

func lastLines(s string, n int) string {
	lines := []string{}
	start := len(s)
	for i := len(s) - 1; i >= 0 && len(lines) < n; i-- {
		if s[i] == '\n' {
			lines = append([]string{s[i+1 : start]}, lines...)
			start = i
		}
	}
	if len(lines) < n && start > 0 {
		lines = append([]string{s[:start]}, lines...)
	}
	out := ""
	for i, l := range lines {
		if i > 0 {
			out += " | "
		}
		out += l
	}
	return out
}

// refreshSync runs the pipeline and reloads the mart. Caller must NOT hold the lock.
func (s *AppState) refreshSync() error {
	s.mu.Lock()
	if s.pipelineRunning {
		s.mu.Unlock()
		return nil
	}
	s.pipelineRunning = true
	s.mu.Unlock()

	err := s.runPipeline()
	if err != nil {
		s.mu.Lock()
		s.lastError = err.Error()
		s.pipelineRunning = false
		s.mu.Unlock()
		return err
	}

	mart, err := loadMart(s.martPath)
	if err != nil {
		s.mu.Lock()
		s.lastError = err.Error()
		s.pipelineRunning = false
		s.mu.Unlock()
		return err
	}

	s.mu.Lock()
	s.mart = mart
	s.lastError = ""
	s.pipelineRunning = false
	s.mu.Unlock()
	return nil
}

// refreshAsync triggers the pipeline in a background goroutine if not already running.
func (s *AppState) refreshAsync() {
	s.mu.RLock()
	running := s.pipelineRunning
	s.mu.RUnlock()
	if running {
		return
	}
	go func() {
		if err := s.refreshSync(); err != nil {
			log.Printf("background refresh error: %v", err)
		}
	}()
}

func (s *AppState) isStale() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.mart == nil || s.mart.UpdatedAt == "" {
		return true
	}
	t, err := time.Parse(time.RFC3339Nano, s.mart.UpdatedAt)
	if err != nil {
		return true
	}
	return time.Since(t) > time.Hour
}

func (s *AppState) addReport(r Report) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	var reports []Report
	if data, err := os.ReadFile(s.reportPath); err == nil {
		_ = json.Unmarshal(data, &reports)
	}
	reports = append(reports, r)
	os.MkdirAll(filepath.Dir(s.reportPath), 0o755)
	out, _ := json.MarshalIndent(reports, "", "  ")
	return os.WriteFile(s.reportPath, out, 0o644)
}

func corsMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next(w, r)
	}
}

func main() {
	root := resolveRoot()
	state := &AppState{
		root:       root,
		martPath:   filepath.Join(root, "data", "mart", "outages.json"),
		reportPath: filepath.Join(root, "data", "reports.json"),
	}

	// Load the latest datamart; if missing, build it once.
	mart, err := loadMart(state.martPath)
	if err != nil {
		log.Printf("mart not found (%v) — running initial pipeline", err)
		if err := state.refreshSync(); err != nil {
			log.Printf("initial pipeline failed: %v", err)
		}
	} else {
		state.mu.Lock()
		state.mart = mart
		state.mu.Unlock()
	}

	// Scheduled refresh every 6 hours.
	go func() {
		ticker := time.NewTicker(6 * time.Hour)
		defer ticker.Stop()
		for range ticker.C {
			state.refreshAsync()
		}
	}()

	// Freshness check loop: if mart is stale (>1h), kick a background refresh.
	go func() {
		ticker := time.NewTicker(15 * time.Minute)
		defer ticker.Stop()
		for range ticker.C {
			if state.isStale() {
				state.refreshAsync()
			}
		}
	}()

	// Serve the exported frontend (out/) on the same port so one tunnel covers
	// both the UI and the API. Falls back to index.html for unknown routes.
	outDir := filepath.Join(root, "out")
	if info, err := os.Stat(outDir); err == nil && info.IsDir() {
		fileServer := http.FileServer(http.Dir(outDir))
		http.Handle("/", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, "/api") {
				http.NotFound(w, r)
				return
			}
			candidate := filepath.Join(outDir, filepath.Clean("/"+r.URL.Path))
			if fi, err := os.Stat(candidate); err != nil || fi.IsDir() {
				http.ServeFile(w, r, filepath.Join(outDir, "index.html"))
				return
			}
			fileServer.ServeHTTP(w, r)
		}))
		log.Printf("serving frontend from %s", outDir)
	} else {
		log.Printf("no %s build found — serve the frontend separately (npm run dev) or run npm run build", outDir)
	}

	http.HandleFunc("/api/mapdata", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		if state.isStale() {
			state.refreshAsync()
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(state.getData())
	}))

	http.HandleFunc("/api/refresh", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost && r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		state.refreshAsync()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprintf(w, `{"status":"refreshing"}`)
	}))

	http.HandleFunc("/api/report", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var rep Report
		if err := json.NewDecoder(r.Body).Decode(&rep); err != nil {
			http.Error(w, "invalid body", http.StatusBadRequest)
			return
		}
		rep.ReportedAt = time.Now().UTC().Format(time.RFC3339)
		if err := state.addReport(rep); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		fmt.Fprintf(w, `{"status":"ok"}`)
	}))

	port := os.Getenv("PORT")
	if port == "" {
		port = "3001"
	}
	log.Printf("backend running on :%s (root=%s)", port, root)
	log.Fatal(http.ListenAndServe(":"+port, nil))
}
