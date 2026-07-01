import math
import colorsys

import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BU3_PATH  = r"C:\Users\chris\iCloudDrive\KIOS\failed\cyprus_BU3_full.gpkg"
BU2_PATH  = r"C:\Users\chris\iCloudDrive\KIOS\failed\cyprus_BU2_full.gpkg"
POLY_PATH = r"C:\Users\chris\iCloudDrive\KIOS\DistrTrSubstThPoly.geojson"

BU2_LABELS = {
    0:  "Unclassified",
    1:  "General building",
    2:  "Cultural venue",
    3:  "Community / government",
    4:  "Public utility office",
    5:  "Town hall",
    6:  "Broadcast station",
    7:  "Health centre / hospital",
    8:  "Nursery / kindergarten",
    9:  "Elementary school",
    10: "Secondary school",
    11: "Higher education",
    12: "Police station",
    13: "Fire station",
    14: "Post office",
    15: "Electricity substation",
    16: "Sports centre / stadium",
    17: "Olympic swimming pool",
    18: "Cemetery",
    19: "Large recreation facility",
    20: "Petroleum storage / refinery",
    21: "Archaeological / heritage site",
    22: "Church / chapel",
    23: "Monastery",
    24: "Unclassified",
    25: "Factory / industrial plant",
    28: "Shopping mall",
    29: "Museum",
    30: "Bank",
    31: "Hotel / apartment building",
    32: "Villa / holiday home",
    33: "Tourist village / resort",
    34: "Mixed religious / community",
    35: "Mosque",
}

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


print("Loading BU3...")
_bu3 = gpd.read_file(BU3_PATH, columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"])
_bu3["B_TYPE"] = _bu3["B_TYPE"].apply(simplify_btype)
_bu3["cx"] = _bu3.geometry.centroid.x
_bu3["cy"] = _bu3.geometry.centroid.y
print(f"  {len(_bu3)} buildings.")

print("Loading BU2...")
_bu2 = gpd.read_file(BU2_PATH, columns=["CLASSIFICATION", "LANDMARKANAMEENG", "SHAPE.STArea()", "geometry"])
_bu2 = _bu2[_bu2["CLASSIFICATION"] != 1].copy()
_bu2["B_TYPE"] = _bu2["CLASSIFICATION"].map(lambda c: BU2_LABELS.get(int(c), "Unclassified") if pd.notna(c) else "Unclassified")
_bu2["FLOOR_QTY"] = 1.0
_bu2["cx"] = _bu2.geometry.centroid.x
_bu2["cy"] = _bu2.geometry.centroid.y
print(f"  {len(_bu2)} buildings (category 1 excluded).")

print("Deduplicating BU3 against BU2 (removing BU3 buildings covered by BU2)...")
_bu3 = _bu3.reset_index(drop=True)
_bu2 = _bu2.reset_index(drop=True)
_bu3_pts = gpd.GeoDataFrame(geometry=_bu3.geometry.centroid, crs=_bu3.crs)
_joined = gpd.sjoin(_bu3_pts, _bu2[["geometry"]], how="inner", predicate="within")
_covered_idx = set(_joined.index.unique())
_bu3_only = _bu3[~_bu3.index.isin(_covered_idx)].copy()
print(f"  Kept {len(_bu3_only)} BU3 buildings (removed {len(_covered_idx)} duplicates).")

_bu3_only["LANDMARKANAMEENG"] = None
COLS = ["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "LANDMARKANAMEENG", "cx", "cy", "geometry"]
_gdf = pd.concat([_bu2[COLS], _bu3_only[COLS]], ignore_index=True)
_gdf = gpd.GeoDataFrame(_gdf, geometry="geometry", crs=_bu2.crs)
print(f"Merged dataset: {len(_gdf)} buildings total.")

print("Loading polygons...")
_polygons = gpd.read_file(POLY_PATH)
if _polygons.crs is None:
    _polygons = _polygons.set_crs("EPSG:4326")
else:
    _polygons = _polygons.to_crs("EPSG:4326")
print(f"Loaded {len(_polygons)} polygons.")

# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_areas(gdf_in: gpd.GeoDataFrame, area_multiplier: float) -> gpd.GeoDataFrame:
    gdf_in = gdf_in.copy()
    gdf_in["living_area"]      = 0.0
    gdf_in["commercial_area"]  = 0.0
    gdf_in["industrial_area"]  = 0.0
    gdf_in["agricultural_area"] = 0.0
    res   = gdf_in["B_TYPE"] == "Residential"
    mixed = gdf_in["B_TYPE"] == "Mixed Use"
    ind   = gdf_in["B_TYPE"] == "Industrial"
    agri  = gdf_in["B_TYPE"] == "Agriculture"
    gdf_in.loc[res, "living_area"] = (
        gdf_in.loc[res, "FLOOR_QTY"] * area_multiplier * gdf_in.loc[res, "SHAPE.STArea()"]
    )
    mixed_total = (
        gdf_in.loc[mixed, "FLOOR_QTY"] * area_multiplier * gdf_in.loc[mixed, "SHAPE.STArea()"]
    )
    gdf_in.loc[mixed, "living_area"]     = mixed_total * 0.5
    gdf_in.loc[mixed, "commercial_area"] = mixed_total * 0.5
    gdf_in.loc[ind, "industrial_area"] = (
        gdf_in.loc[ind, "FLOOR_QTY"] * gdf_in.loc[ind, "SHAPE.STArea()"]
    )
    gdf_in.loc[agri, "agricultural_area"] = (
        gdf_in.loc[agri, "FLOOR_QTY"] * gdf_in.loc[agri, "SHAPE.STArea()"]
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
    area_multiplier: float = 0.5


class BuildingsRequest(BaseModel):
    poly_idx: int
    b_type: str


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
            "agricultural_area": 0.0,
            "by_type": [],
            "buildings": [],
        }

    within = calc_areas(within, req.area_multiplier)

    by_type = (
        within.groupby("B_TYPE")
        .agg(count=("B_TYPE", "size"), living_area=("living_area", "sum"))
        .reset_index()
        .rename(columns={"B_TYPE": "type"})
        .round({"living_area": 1})
        .to_dict(orient="records")
    )

    bld = within[["cx", "cy", "B_TYPE", "LANDMARKANAMEENG"]].rename(
        columns={"cx": "lon", "cy": "lat", "B_TYPE": "b_type", "LANDMARKANAMEENG": "name"}
    )
    bld["name"] = bld["name"].where(bld["name"].notna(), other=None)
    buildings = bld.to_dict(orient="records")

    return {
        "building_count": len(within),
        "residents": int(within["residents"].sum()),
        "living_area": round(float(within["living_area"].sum()), 1),
        "commercial_area": round(float(within["commercial_area"].sum()), 1),
        "industrial_area": round(float(within["industrial_area"].sum()), 1),
        "agricultural_area": round(float(within["agricultural_area"].sum()), 1),
        "by_type": by_type,
        "buildings": buildings,
    }


@app.post("/buildings")
def get_buildings_by_type(req: BuildingsRequest):
    """Return individual building details for a specific type within a polygon."""
    if req.poly_idx < 0 or req.poly_idx >= len(_polygons):
        raise HTTPException(status_code=404, detail="Polygon index out of range")

    poly_geom = _polygons.iloc[req.poly_idx].geometry
    bounds = poly_geom.bounds
    bbox = _gdf.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    within = bbox[bbox.centroid.within(poly_geom)]
    subset = within[within["B_TYPE"] == req.b_type].copy()

    subset = subset.copy()
    subset["FLOOR_QTY"] = subset["FLOOR_QTY"].fillna(1).astype(int)
    subset["LANDMARKANAMEENG"] = subset["LANDMARKANAMEENG"].where(subset["LANDMARKANAMEENG"].notna(), other=None)
    out = subset[["LANDMARKANAMEENG", "SHAPE.STArea()", "FLOOR_QTY", "cy", "cx"]].copy()
    out = out.round({"SHAPE.STArea()": 1, "cy": 6, "cx": 6})
    out = out.rename(columns={
        "LANDMARKANAMEENG": "name",
        "SHAPE.STArea()": "area_m2",
        "FLOOR_QTY": "floors",
        "cy": "lat",
        "cx": "lon",
    })

    return {"type": req.b_type, "count": len(out), "buildings": out.to_dict(orient="records")}