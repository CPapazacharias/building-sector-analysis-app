import math
import colorsys

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

GPKG_PATH = r"C:\Users\chris\iCloudDrive\KIOS\failed\cyprus_BU3_full.gpkg"
POLY_PATH  = r"C:\Users\chris\iCloudDrive\KIOS\DistrTrSubstThPoly.geojson"

app = FastAPI(title="Building Sector Analysis API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Data loaded once at startup ───────────────────────────────────────────────

def simplify_btype(val):
    v = val.strip().lower()
    if "mixed" in v:   return "Mixed Use"
    if "residen" in v: return "Residential"
    if "industr" in v: return "Industrial"
    if "agri" in v:    return "Agriculture"
    return val.strip()


print("Loading building data...")
_gdf = gpd.read_file(GPKG_PATH, columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"])
_gdf["B_TYPE"] = _gdf["B_TYPE"].apply(simplify_btype)
_gdf["cx"] = _gdf.geometry.centroid.x
_gdf["cy"] = _gdf.geometry.centroid.y
print(f"Loaded {len(_gdf)} buildings.")

print("Loading polygons...")
_polygons = gpd.read_file(POLY_PATH)
if _polygons.crs is None:
    _polygons = _polygons.set_crs("EPSG:4326")
else:
    _polygons = _polygons.to_crs("EPSG:4326")
print(f"Loaded {len(_polygons)} polygons.")

# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_areas(gdf_in: gpd.GeoDataFrame, floor_multiplier: float) -> gpd.GeoDataFrame:
    gdf_in = gdf_in.copy()
    gdf_in["living_area"]     = 0.0
    gdf_in["commercial_area"] = 0.0
    gdf_in["industrial_area"] = 0.0
    res   = gdf_in["B_TYPE"] == "Residential"
    mixed = gdf_in["B_TYPE"] == "Mixed Use"
    ind   = gdf_in["B_TYPE"] == "Industrial"
    gdf_in.loc[res, "living_area"] = (
        gdf_in.loc[res, "FLOOR_QTY"] * floor_multiplier * gdf_in.loc[res, "SHAPE.STArea()"]
    )
    mixed_total = (
        gdf_in.loc[mixed, "FLOOR_QTY"] * floor_multiplier * gdf_in.loc[mixed, "SHAPE.STArea()"]
    )
    gdf_in.loc[mixed, "living_area"]     = mixed_total * 0.5
    gdf_in.loc[mixed, "commercial_area"] = mixed_total * 0.5
    gdf_in.loc[ind, "industrial_area"] = (
        gdf_in.loc[ind, "FLOOR_QTY"] * gdf_in.loc[ind, "SHAPE.STArea()"]
    )
    gdf_in["residents"] = (gdf_in["living_area"] / 75).round(0).astype(int)
    return gdf_in


def poly_coords(geom):
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return [[x, y] for x, y in geom.exterior.coords]


# ── Schemas ───────────────────────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    poly_idx: int
    floor_multiplier: float = 0.5


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/polygons")
def get_polygons():
    """Return all polygon geometries and SCADASUBSTSHORTID for the map."""
    features = []
    for idx, row in _polygons.iterrows():
        features.append({
            "idx": int(idx),
            "scada_id": row.get("SCADASUBSTSHORTID", ""),
            "polygon": poly_coords(row.geometry),
        })
    return features


@app.get("/polygon_lookup")
def get_polygon_lookup():
    """Return mapping of SCADASUBSTSHORTID → polygon row index."""
    return {row["SCADASUBSTSHORTID"]: int(idx) for idx, row in _polygons.iterrows()}


@app.post("/analyse")
def analyse(req: AnalyseRequest):
    """Query buildings within a polygon and return aggregated metrics."""
    if req.poly_idx < 0 or req.poly_idx >= len(_polygons):
        raise HTTPException(status_code=404, detail="Polygon index out of range")

    poly_geom = _polygons.iloc[req.poly_idx].geometry
    bounds = poly_geom.bounds
    bbox = _gdf.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    within = bbox[bbox.centroid.within(poly_geom)].copy()

    if within.empty:
        return {
            "building_count": 0,
            "residents": 0,
            "living_area": 0.0,
            "commercial_area": 0.0,
            "industrial_area": 0.0,
            "by_type": [],
            "buildings": [],
        }

    within = calc_areas(within, req.floor_multiplier)

    by_type = (
        within.groupby("B_TYPE")
        .agg(count=("B_TYPE", "size"), living_area=("living_area", "sum"))
        .reset_index()
        .rename(columns={"B_TYPE": "type"})
        .round({"living_area": 1})
        .to_dict(orient="records")
    )

    buildings = within[["cx", "cy", "B_TYPE"]].rename(
        columns={"cx": "lon", "cy": "lat", "B_TYPE": "b_type"}
    ).to_dict(orient="records")

    return {
        "building_count": len(within),
        "residents": int(within["residents"].sum()),
        "living_area": round(float(within["living_area"].sum()), 1),
        "commercial_area": round(float(within["commercial_area"].sum()), 1),
        "industrial_area": round(float(within["industrial_area"].sum()), 1),
        "by_type": by_type,
        "buildings": buildings,
    }