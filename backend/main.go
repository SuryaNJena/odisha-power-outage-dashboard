package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// circleToDistrict maps the "Name of Circle" CSV column to an Odisha district name
var circleToDistrict = map[string]string{
	"BBSR-1":         "Khordha",
	"BBSR-2":         "Khordha",
	"CUTTACK":        "Cuttack",
	"PARADEEP":       "Jagatsinghpur",
	"DHENKANAL":      "Dhenkanal",
	"BHADRAK":        "Bhadrak",
	"BARIPADA":       "Mayurbhanj",
	"BALASORE":       "Balasore",
	"KEONJHAR":       "Keonjhar",
	"JAJPUR":         "Jajpur",
	"BARGARH":        "Bargarh",
	"SAMBALPUR":      "Sambalpur",
	"ROURKELA":       "Sundargarh",
	"BOLANGIR":       "Bolangir",
	"KALAHANDI":      "Kalahandi",
	"JEYPORE":        "Koraput",
	"BERHAMPUR":      "Ganjam",
	"BERHAMPUR CITY": "Ganjam",
	"ASKA":           "Ganjam",
	"BHANJANAGAR":    "Kandhamal",
	"RAYAGADA":       "Rayagada",
	"JHARSUGUDA":     "Jharsuguda",
	"KENDRAPARA":     "Kendrapara",
	"NAYAGARH":       "Nayagarh",
	"PURI":           "Puri",
	"GANJAM":         "Ganjam",
	"KORAPUT":        "Koraput",
	"MALKANGIRI":     "Malkangiri",
	"NABARANGPUR":    "Nabarangpur",
	"NUAPADA":        "Nuapada",
	"SONEPUR":        "Subarnapur",
	"ANGUL":          "Angul",
	"BAUDH":          "Boudh",
	"GAJAPATI":       "Gajapati",
	"KENDUJHAR":      "Keonjhar",
	"SUBARNAPUR":     "Subarnapur",
}

type DistrictData struct {
	Name  string `json:"name"`
	Value int    `json:"value"`
}

type MapResponse struct {
	Districts []DistrictData `json:"districts"`
	Total     int            `json:"total"`
}

func loadCSVData(dirPath string) (map[string]int, error) {
	counts := make(map[string]int)

	entries, err := os.ReadDir(dirPath)
	if err != nil {
		return nil, fmt.Errorf("reading scrappedData dir: %w", err)
	}

	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".csv") {
			continue
		}

		fpath := filepath.Join(dirPath, entry.Name())
		f, err := os.Open(fpath)
		if err != nil {
			log.Printf("Warning: could not open %s: %v", fpath, err)
			continue
		}

		reader := csv.NewReader(f)
		reader.LazyQuotes = true
		reader.FieldsPerRecord = -1 // variable fields

		rows, err := reader.ReadAll()
		f.Close()
		if err != nil {
			log.Printf("Warning: error reading %s: %v", fpath, err)
			continue
		}

		// Skip header row; column index 1 = "Name of Circle"
		for i, row := range rows {
			if i == 0 || len(row) < 2 {
				continue
			}
			circle := strings.TrimSpace(strings.ToUpper(row[1]))
			district, ok := circleToDistrict[circle]
			if !ok {
				// Try title-case lookup
				titleCircle := strings.Title(strings.ToLower(row[1]))
				district, ok = circleToDistrict[titleCircle]
			}
			if ok {
				counts[district]++
			} else {
				log.Printf("Unknown circle: %q (from %s)", row[1], entry.Name())
			}
		}
	}

	return counts, nil
}

var cachedData *MapResponse

func loadData() {
	// When launched via `go run backend/main.go` from the project root,
	// the working directory is the project root.
	// Try both locations to be safe.
	scrappedDir := "scrappedData"
	if _, err := os.Stat(scrappedDir); os.IsNotExist(err) {
		scrappedDir = filepath.Join("..", "scrappedData")
	}
	counts, err := loadCSVData(scrappedDir)
	if err != nil {
		log.Printf("Error loading CSV data: %v — using empty data", err)
		counts = make(map[string]int)
	}

	districts := make([]DistrictData, 0, len(counts))
	total := 0
	for name, count := range counts {
		districts = append(districts, DistrictData{Name: name, Value: count})
		total += count
	}
	sort.Slice(districts, func(i, j int) bool {
		return districts[i].Value > districts[j].Value
	})

	cachedData = &MapResponse{
		Districts: districts,
		Total:     total,
	}
	log.Printf("Loaded %d outage records across %d districts", total, len(districts))
}

func corsMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next(w, r)
	}
}

func getMapData(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if cachedData == nil {
		http.Error(w, "data not loaded", http.StatusInternalServerError)
		return
	}
	json.NewEncoder(w).Encode(cachedData)
}

func main() {
	loadData()

	http.Handle("/", http.FileServer(http.Dir("../public")))
	http.HandleFunc("/api/mapdata", corsMiddleware(getMapData))

	port := os.Getenv("PORT")
	if port == "" {
		port = "3001"
	}

	fmt.Printf("Server starting on port %s\n", port)
	log.Fatal(http.ListenAndServe(":"+port, nil))
}
