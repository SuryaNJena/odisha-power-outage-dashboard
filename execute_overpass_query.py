import requests
import json
import os

def fetch_and_save_geojson(output_file="export.json"):
    """
    Fetches data from Overpass API and saves it as a GeoJSON file.
    """
    overpass_query = f"""
    [out:json][timeout:250];

    area[name="Odisha"][admin_level="4"]->.odisha;
    (
        rel(area.odisha) ["boundary"="administrative"] ["admin_level"~"^(4)$"];
        
    );
    out geom;
    """

    api_url = f"https://overpass-api.de/api/interpreter?data={overpass_query}"

    try:
        response = requests.get(api_url)
        response.raise_for_status()
        geojson_data = response.json()

        with open(output_file, "w", encoding="utf-8") as file:
            json.dump(geojson_data, file, indent=2, ensure_ascii=False)
            print(f"Saved data to: {output_file}")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching from Overpass API: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    output_file_name = "./public/export.json"
    fetch_and_save_geojson(output_file_name)
    os.system("npx osmtogeojson ./public/export.json > ./public/export.geojson")
