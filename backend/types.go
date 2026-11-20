package main

import "time"

// Wire types shared by the API and the snapshot fallback. JSON field names are
// a contract with the Next.js frontend (see platform/CONTRACT.md).

type DistrictData struct {
	Name  string `json:"name"`
	Value int    `json:"value"`
}

type Dot struct {
	District          string   `json:"district"`
	Circle            string   `json:"circle"`
	Division          string   `json:"division"`
	GSS               string   `json:"gss"`
	Voltage           string   `json:"voltage"`
	Feeder            string   `json:"feeder"`
	Start             string   `json:"start"`
	End               string   `json:"end"`
	Date              string   `json:"date"`
	Reason            string   `json:"reason"`
	Areas             string   `json:"areas"`
	Lat               float64  `json:"lat"`
	Lon               float64  `json:"lon"`
	Geocoded          bool     `json:"geocoded"`
	IsPlanned         *bool    `json:"isPlanned,omitempty"`
	DurationMinutes   *float64 `json:"durationMinutes,omitempty"`
	AffectedCustomers *int     `json:"affectedCustomers,omitempty"`
	Cause             *string  `json:"cause,omitempty"`
	ETR               *string  `json:"etr,omitempty"`
}

type MapResponse struct {
	Districts []DistrictData `json:"districts"`
	Dots      []Dot          `json:"dots"`
	Total     int            `json:"total"`
	UpdatedAt string         `json:"updatedAt"`
	Source    string         `json:"source"`
}

type RealtimeTotals struct {
	Reports         int `json:"reports"`
	Districts       int `json:"districts"`
	Alerts          int `json:"alerts"`
	OfficialOutages int `json:"officialOutages"`
}

type RealtimeDistrict struct {
	District           string  `json:"district"`
	Reports            int     `json:"reports"`
	UniqueReporters    int     `json:"uniqueReporters"`
	OfficialOutages    int     `json:"officialOutages"`
	UnconfirmedReports int     `json:"unconfirmedReports"`
	RiskScore          float64 `json:"riskScore"`
	RiskLevel          string  `json:"riskLevel"`
	UpdatedAt          string  `json:"updatedAt"`
}

type Alert struct {
	AlertID     string `json:"alertId"`
	District    string `json:"district"`
	AlertType   string `json:"alertType"`
	Severity    string `json:"severity"`
	Message     string `json:"message"`
	ReportCount int    `json:"reportCount"`
	WindowStart string `json:"windowStart"`
	WindowEnd   string `json:"windowEnd"`
	CreatedAt   string `json:"createdAt"`
}

type TrendPoint struct {
	Bucket  string `json:"bucket"`
	Reports int    `json:"reports"`
}

type PipelineStatus struct {
	Kafka           string `json:"kafka"`
	Flink           string `json:"flink"`
	Spark           string `json:"spark"`
	Airflow         string `json:"airflow"`
	LastSparkUpdate string `json:"lastSparkUpdate"`
}

type RealtimeResponse struct {
	Available     bool               `json:"available"`
	UpdatedAt     string             `json:"updatedAt"`
	WindowMinutes int                `json:"windowMinutes"`
	Totals        RealtimeTotals     `json:"totals"`
	Districts     []RealtimeDistrict `json:"districts"`
	Alerts        []Alert            `json:"alerts"`
	Trend         []TrendPoint       `json:"trend"`
	Pipeline      PipelineStatus     `json:"pipeline"`
	Message       string             `json:"message,omitempty"`
}

type ReliabilityDistrict struct {
	District          string  `json:"district"`
	SAIDIMinutes      float64 `json:"saidiMinutes"`
	SAIFI             float64 `json:"saifi"`
	CAIDIMinutes      float64 `json:"caidiMinutes"`
	ASAIPct           float64 `json:"asaiPct"`
	OutageCount       int     `json:"outageCount"`
	CustomersAffected int64   `json:"customersAffected"`
}

type ReliabilityTrend struct {
	Date         string  `json:"date"`
	SAIDIMinutes float64 `json:"saidiMinutes"`
	SAIFI        float64 `json:"saifi"`
}

type ReliabilityResponse struct {
	Available bool                  `json:"available"`
	Days      int                   `json:"days"`
	Districts []ReliabilityDistrict `json:"districts"`
	Trend     []ReliabilityTrend    `json:"trend"`
	Message   string                `json:"message,omitempty"`
}

type RiskDistrict struct {
	District        string  `json:"district"`
	RiskScore       float64 `json:"riskScore"`
	RiskLevel       string  `json:"riskLevel"`
	ExpectedOutages float64 `json:"expectedOutages"`
	MaxPrecipMM     float64 `json:"maxPrecipMm"`
	MaxWindKPH      float64 `json:"maxWindKph"`
	ForecastDate    string  `json:"forecastDate"`
}

type RiskResponse struct {
	Available bool           `json:"available"`
	UpdatedAt string         `json:"updatedAt"`
	Districts []RiskDistrict `json:"districts"`
	Message   string         `json:"message,omitempty"`
}

// MapReport is a district-level aggregate for public display.
type MapReport struct {
	District string    `json:"district"`
	Lat      float64   `json:"lat"`
	Lon      float64   `json:"lon"`
	Count    int       `json:"count"`
	LatestAt time.Time `json:"latestAt"`
}

type ReportInput struct {
	Kind     string   `json:"kind"`
	District string   `json:"district"`
	GSS      string   `json:"gss"`
	Feeder   string   `json:"feeder"`
	Comment  string   `json:"comment"`
	Severity int      `json:"severity"`
	Lat      *float64 `json:"lat"`
	Lon      *float64 `json:"lon"`
}

type HealthComponent struct {
	Status    string `json:"status"`
	LatencyMS int64  `json:"latencyMs"`
	Detail    string `json:"detail,omitempty"`
}

type HealthResponse struct {
	Status     string                     `json:"status"`
	Version    string                     `json:"version"`
	UptimeSec  int64                      `json:"uptimeSec"`
	Components map[string]HealthComponent `json:"components"`
}
