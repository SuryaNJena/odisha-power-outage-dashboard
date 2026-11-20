package main

import "testing"

func TestResolveReportAreaUsesSelectedDistrict(t *testing.T) {
	place, ok := reportAreas.resolve("Khurda", "Unit III")
	if !ok || place.Area != "Unit-3" || place.District != "Khordha" {
		t.Fatalf("alias should resolve to Unit-3 in Khordha, got %+v", place)
	}
	if _, ok := reportAreas.resolve("Cuttack", "Unit III"); ok {
		t.Fatal("locality must not be matched in the wrong district")
	}
	if _, ok := reportAreas.resolve("Khordha", "999 Invented Lane"); ok {
		t.Fatal("unmatched address must remain district-level")
	}
}

func TestOfficialAreaFallbackOnlyForMissingBhubaneswarStation(t *testing.T) {
	dot := Dot{District: "Khordha", Division: "BCDD-I, BBSR", GSS: "SATYANAGAR", Lat: 20.09, Lon: 85.51}
	precise, outside := dot, dot
	precise.Geocoded = true
	outside.Division = "KHED, KHORDHA"
	data := MapResponse{Dots: []Dot{dot, precise, outside}}
	applyOfficialAreaFallbacks(&data)
	matched := data.Dots[0]
	if matched.LocationArea != "Satya Nagar" || matched.Geocoded || matched.Lat == dot.Lat {
		t.Fatalf("expected approximate Satya Nagar fallback, got %+v", matched)
	}
	if data.Dots[1] != precise || data.Dots[2] != outside {
		t.Fatal("fallback must preserve station matches and unrelated outages")
	}
}
