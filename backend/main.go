package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
)

func getMapData(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	// For now, return a sample data structure
	// In production, this would read from CSV and return actual data
	data := map[string]interface{}{
		"districts": []map[string]interface{}{
			{"name": "Kendrapara", "value": 75},
			{"name": "Jajpur", "value": 85},
			{"name": "Khordha", "value": 65},
			{"name": "Puri", "value": 90},
			{"name": "Cuttack", "value": 80},
			{"name": "Bhadrak", "value": 70},
			{"name": "Mayurbhanj", "value": 60},
			{"name": "Kalahandi", "value": 55},
			{"name": "Boudh", "value": 45},
			{"name": "Gajapati", "value": 35},
			{"name": "Rayagada", "value": 40},
			{"name": "Nabarangpur", "value": 50},
			{"name": "Koraput", "value": 45},
			{"name": "Malkangiri", "value": 30},
			{"name": "Kandhamal", "value": 40},
			{"name": "Bargarh", "value": 55},
			{"name": "Sambalpur", "value": 65},
			{"name": "Jharsuguda", "value": 70},
			{"name": "Deogarh", "value": 60},
			{"name": "Sundargarh", "value": 55},
			{"name": "Jharsuguda", "value": 70},
			{"name": "Jagatsinghpur", "value": 80},
			{"name": "Nayagarh", "value": 75},
			{"name": "Angul", "value": 65},
			{"name": "Dhenkanal", "value": 70},
			{"name": "Kendujhar", "value": 60},
			{"name": "Sonepur", "value": 55},
			{"name": "Bolangir", "value": 50},
		},
	}
	
	json.NewEncoder(w).Encode(data)
}

func main() {
	// Serve static files
	http.Handle("/", http.FileServer(http.Dir("../public")))
	
	// API endpoints
	http.HandleFunc("/api/mapdata", getMapData)
	
	// Start server
	port := os.Getenv("PORT")
	if port == "" {
		port = "3001" // Default port
	}
	
	fmt.Printf("Server starting on port %s\n", port)
	log.Fatal(http.ListenAndServe(":"+port, nil))
}
