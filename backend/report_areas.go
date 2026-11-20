package main

import (
	_ "embed"
	"encoding/json"
	"strings"
	"unicode"
)

// Generated from Odisha boundary data and curated place references.
//
//go:embed report_areas.json
var reportAreaData []byte

type reportPlace struct {
	Area             string
	District         string
	Lat              float64
	Lon              float64
	Aliases          []string
	OfficialStations []string
}

func placeKey(name string) string {
	return strings.Map(func(c rune) rune {
		if unicode.IsLetter(c) || unicode.IsNumber(c) {
			return unicode.ToLower(c)
		}
		return -1
	}, name)
}

type areaCatalog struct {
	districtNames    map[string]string
	areas            map[[2]string]reportPlace
	officialStations map[[2]string]reportPlace
}

var reportAreas = loadReportAreas()

func loadReportAreas() areaCatalog {
	var data struct {
		DistrictNames map[string]string
		Areas         []reportPlace
	}
	if err := json.Unmarshal(reportAreaData, &data); err != nil {
		panic(err)
	}
	catalog := areaCatalog{data.DistrictNames, make(map[[2]string]reportPlace), make(map[[2]string]reportPlace)}
	for _, area := range data.Areas {
		catalog.areas[[2]string{area.District, placeKey(area.Area)}] = area
	}
	for _, area := range data.Areas {
		for _, alias := range area.Aliases {
			catalog.areas[[2]string{area.District, placeKey(alias)}] = area
		}
		for _, station := range area.OfficialStations {
			catalog.officialStations[[2]string{area.District, placeKey(station)}] = area
		}
	}
	return catalog
}

func (c areaCatalog) resolve(district, area string) (reportPlace, bool) {
	if canonical := c.districtNames[placeKey(district)]; canonical != "" {
		district = canonical
	}
	place, ok := c.areas[[2]string{district, placeKey(area)}]
	return place, ok
}

func applyOfficialAreaFallbacks(m *MapResponse) {
	for i := range m.Dots {
		d := &m.Dots[i]
		if d.Geocoded || !strings.Contains(strings.ToUpper(d.Division), "BBSR") {
			continue
		}
		if area, ok := reportAreas.officialStations[[2]string{d.District, placeKey(d.GSS)}]; ok {
			d.Lat, d.Lon, d.LocationArea = area.Lat, area.Lon, area.Area
		}
	}
}
