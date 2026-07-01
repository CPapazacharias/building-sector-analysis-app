# Building Sector Analysis — Code Documentation

## Overview

The project analyses Cyprus building data by substation coverage area. It loads two building datasets (BU3 and BU2), merges them, and lets the user select a substation zone to see building counts, estimated residents, and area breakdowns.

There are two versions of the app:

| Version | Branch | Files | How to run |
|---|---|---|---|
| Split (frontend + backend) | `master` / `split` | `backend.py` + `frontend.py` | Run both separately |
| Merged (single file) | `not_split` | `app.py` | Run with Streamlit only |

The split version is better for development (the heavy data loading only happens once in the backend process). The merged version is simpler to deploy.

---

## Data Sources

### BU3 — Main building dataset
- **File:** `cyprus_BU3_full.gpkg`
- **Records:** ~564,000 buildings
- **Key fields:**
  - `B_TYPE` — building type label (e.g. "LowResidential Building with inclined roof") — simplified to Residential / Mixed Use / Industrial / Agriculture
  - `FLOOR_QTY` — number of floors
  - `SHAPE.STArea()` — footprint area in m²

### BU2 — Landmark / public building dataset
- **File:** `cyprus_BU2_full.gpkg`
- **Records:** ~62,700 buildings (reduced to ~6,500 after excluding category 1)
- **Key fields:**
  - `CLASSIFICATION` — numeric code (0–35) mapped to a descriptive label (see BU2_LABELS)
  - `LANDMARKANAMEENG` — English name of the building (e.g. "Nicosia General Hospital")
  - `SHAPE.STArea()` — footprint area in m²
- **Category 1 excluded** — code 1 is "General building", a generic fallback used for 90% of BU2 records. These would double-count BU3 buildings.

### Thiessen Polygons
- **File:** `DistrTrSubstThPoly.geojson`
- **Records:** 52 polygons
- **Purpose:** Each polygon represents the coverage area of one electricity substation, generated as Voronoi/Thiessen polygons
- **Key field:** `SCADASUBSTSHORTID` — short ID matching the `scada_id` column in the substations CSV

---

## Backend (`backend.py`)

The backend is a **FastAPI** server. It loads all data once at startup and keeps it in memory. All four API endpoints read from that in-memory data.

### Startup — Data Loading

#### Step 1: Load BU3
```python
_bu3 = gpd.read_file(BU3_PATH, columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"])
```
Only the four needed columns are loaded to save memory. The `B_TYPE` string is then simplified using `simplify_btype()`.

#### Step 2: Load BU2
```python
_bu2 = gpd.read_file(BU2_PATH, columns=["CLASSIFICATION", "LANDMARKANAMEENG", "SHAPE.STArea()", "geometry"])
_bu2 = _bu2[_bu2["CLASSIFICATION"] != 1].copy()
```
Category 1 buildings are dropped immediately. The numeric `CLASSIFICATION` code is then mapped to a human-readable label via `BU2_LABELS`.

#### Step 3: Deduplicate BU3 against BU2
BU3 and BU2 overlap — the same physical building can appear in both datasets. The rule is: **keep the BU2 version**, discard the BU3 version.

```python
_bu3_pts = gpd.GeoDataFrame(geometry=_bu3.geometry.centroid, crs=_bu3.crs)
_joined = gpd.sjoin(_bu3_pts, _bu2[["geometry"]], how="inner", predicate="within")
_covered_idx = set(_joined.index.unique())
_bu3_only = _bu3[~_bu3.index.isin(_covered_idx)].copy()
```

How it works:
1. Take the centroid (centre point) of every BU3 building
2. Spatial join: find which centroids fall **inside** a BU2 polygon
3. Collect those BU3 indices and exclude them
4. Result: only BU3 buildings that are NOT already represented in BU2

This removes ~6,700 BU3 buildings, leaving ~557,700 BU3-only buildings.

#### Step 4: Merge into one GeoDataFrame
```python
_gdf = pd.concat([_bu2[COLS], _bu3_only[COLS]], ignore_index=True)
```
BU2 buildings come first (so their index positions are 0 to 6540), then the remaining BU3 buildings. The final merged dataset has ~564,000 buildings.

#### Step 5: Load polygons
The 52 Thiessen polygons are loaded and converted to WGS84 (EPSG:4326) coordinate system to match the web map.

---

### Helper Functions

#### `simplify_btype(val)`
Converts verbose BU3 type strings into four clean categories:
- "Residential" — anything with "residen" in the name
- "Mixed Use" — anything with "mixed"
- "Industrial" — anything with "industr"
- "Agriculture" — anything with "agri"
- Anything else is left as-is

#### `calc_areas(gdf_in, area_multiplier)`
Computes living area, commercial area, industrial area, and agricultural area for each building.

- **Residential:** `living_area = FLOOR_QTY × area_multiplier × footprint_area`
- **Mixed Use:** split 50/50 between living_area and commercial_area
- **Industrial:** `industrial_area = FLOOR_QTY × footprint_area` (no area multiplier)
- **Agriculture:** `agricultural_area = FLOOR_QTY × footprint_area`
- **Estimated residents:** `living_area / 75` (assumes 75 m² per person)

The **area multiplier** (default 0.5) accounts for the fact that not all of a building's footprint is enclosed usable space — verandas, balconies, and covered external areas are included in the footprint measurement but are not interior living space.

#### `poly_coords(geom)`
Converts a Shapely polygon geometry into a list of `[lon, lat]` coordinate pairs for the pydeck map. For MultiPolygon geometries, it takes the largest part.

---

### API Endpoints

#### `GET /polygons`
Returns the geometry and SCADA ID of all 52 Thiessen polygons. Used by the frontend to draw the polygon outlines on the map.

Response format:
```json
[{"idx": 0, "scada_id": "NIKO", "polygon": [[lon, lat], ...]}, ...]
```

#### `GET /polygon_lookup`
Returns a dictionary mapping SCADA IDs to polygon row indices. Used to match uploaded substation CSV entries to polygons.

Response format:
```json
{"NIKO": 0, "LIMO": 1, ...}
```

#### `POST /analyse`
The main analysis endpoint. Takes a polygon index and area multiplier, returns all building metrics for that zone.

Request:
```json
{"poly_idx": 5, "area_multiplier": 0.5}
```

How it works:
1. Get the polygon geometry for the requested index
2. Use `.cx[...]` bounding box filter to quickly narrow candidates
3. Filter to buildings whose centroid falls **within** the polygon
4. Run `calc_areas()` on the result
5. Group by `B_TYPE` for the breakdown table
6. Return all individual building coordinates for the map dots

Response includes: `building_count`, `residents`, `living_area`, `commercial_area`, `industrial_area`, `agricultural_area`, `by_type` (list), `buildings` (list of lat/lon/type/name).

#### `POST /buildings`
On-demand detail endpoint — only called when the user clicks a row in the Count per B_TYPE table. Returns individual building records for a specific type within a polygon.

Request:
```json
{"poly_idx": 5, "b_type": "Health centre / hospital"}
```

Response includes a list of buildings with: `name` (LANDMARKANAMEENG), `area_m2`, `floors`, `lat`, `lon`.

---

## Frontend (`frontend.py`)

The frontend is a **Streamlit** app. It calls the backend API and renders the map and analytics.

### Session State

Streamlit re-runs the entire script on every user interaction. Session state persists values between re-runs:

| Key | Purpose |
|---|---|
| `zones` | List of substation zones (name, lat, lon, poly_idx) |
| `selected_zone` | Index of the currently selected zone |
| `analysis_cache` | Dict caching API results to avoid repeat calls |
| `last_upload_id` | Tracks which CSV file was last uploaded |
| `selected_btype` | Which B_TYPE row the user clicked in the detail table |
| `detail_poly` | Which polygon the B_TYPE selection belongs to (cleared on polygon change) |

### Sidebar

- **Area multiplier slider** — value between 0.1 and 1.0, passed to `/analyse`
- **CSV upload** — accepts a CSV with columns `name, lat, lon, scada_id`. Each row becomes a zone; `scada_id` is looked up in `poly_lookup` to find the matching polygon index
- **Substation selector** — dropdown to switch between zones
- **Name / Lat / Lon inputs** — manual zone editing
- **Add / Remove buttons** — manage the zone list

### Analysis Flow

```
User selects zone
    → poly_idx from zone dict
    → check analysis_cache[(poly_idx, area_multiplier)]
    → if not cached: POST /analyse → store result
    → use cached result for map + analytics
```

### Map Layers (pydeck)

The map is built from four stacked layers:

1. **PolygonLayer** — draws the Thiessen polygon outlines. The selected polygon gets a brighter fill and thicker border. All loaded zones are shown, not just the selected one.

2. **ScatterplotLayer (buildings)** — one dot per building in the selected polygon, coloured by `B_TYPE`. Dot size is capped between 1px and 12px so clusters remain readable at any zoom level.

3. **ScatterplotLayer (pins)** — one larger dot per substation zone at its lat/lon coordinates, coloured by zone index (each zone gets a unique hue).

4. **TextLayer (labels)** — substation name labels above each pin.

The map view auto-zooms to fit all loaded zones using a log-scale calculation on the lat/lon span.

### Analytics Section

Below the map, five columns are shown when a polygon has results:

| Column | Contents |
|---|---|
| `col_m1` | Buildings count, Estimated residents, Residential area |
| `col_m2` | Commercial area, Industrial area, Agricultural area, Health/hospital area |
| `col_pie` | Donut pie chart of building counts by type |
| `col_a` | Clickable Count per B_TYPE table (excluding Residential) |
| `col_b` | Living area per B_TYPE table |

### Pie Chart Grouping

Several granular BU2 categories are merged into broader groups **only for the pie chart display**. The underlying data and the detail table still use the original labels.

| Original labels | Pie chart group |
|---|---|
| Nursery / kindergarten, Elementary school, Secondary school, Higher education | Education |
| Church / chapel, Monastery, Mosque, Mixed religious / community | Religious |
| Police station, Post office, Public utility office | Public services |

### Building Detail (click to expand)

When the user clicks a row in the Count per B_TYPE table:
1. `selected_btype` is stored in session state
2. A `POST /buildings` request is made to the backend (only once — result is cached)
3. An expandable table appears below showing individual buildings of that type with name, area, floors, and coordinates

Residential is excluded from the clickable table because there are too many individual residential buildings to display usefully.

---

## Merged Version (`app.py`)

`app.py` on the `not_split` branch combines everything into a single Streamlit file. The differences from the split version:

- `@st.cache_resource` replaces module-level loading. This decorator caches the return value for the lifetime of the server process — the GeoDataFrames are loaded once and reused across all user sessions.
- API endpoints are replaced with direct Python function calls (`query_polygon`, `query_buildings_by_type`)
- No HTTP requests — everything runs in the same process
- Run with: `py -3.14 -m streamlit run app.py`

The logic is identical to the split version — only the transport layer changes.

---

## How to Run (Split Version)

**Terminal 1 — Backend:**
```
cd C:\Users\chris\iCloudDrive\KIOS\building-sector-analysis
py -3.14 -m uvicorn backend:app --reload
```

**Terminal 2 — Frontend:**
```
cd C:\Users\chris\iCloudDrive\KIOS\building-sector-analysis
py -3.14 -m streamlit run frontend.py
```

The backend runs on `http://localhost:8000`. The frontend runs on `http://localhost:8501` and calls the backend automatically.

## How to Run (Merged Version)

```
cd C:\Users\chris\iCloudDrive\KIOS\building-sector-analysis
py -3.14 -m streamlit run app.py
```