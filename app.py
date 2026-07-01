import math
import colorsys
import io
import warnings
warnings.filterwarnings("ignore")

import geopandas as gpd
import pandas as pd
import pydeck as pdk
import streamlit as st
import altair as alt

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

BTYPE_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8",
    "#f58231", "#911eb4", "#42d4f4", "#f032e6",
    "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3",
]

PIE_GROUPS = {
    "Nursery / kindergarten":      "Education",
    "Elementary school":           "Education",
    "Secondary school":            "Education",
    "Higher education":            "Education",
    "Church / chapel":             "Religious",
    "Monastery":                   "Religious",
    "Mosque":                      "Religious",
    "Mixed religious / community": "Religious",
    "Police station":              "Public services",
    "Post office":                 "Public services",
    "Public utility office":       "Public services",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]


def zone_color(i, n):
    hue = i / max(n, 1)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
    return [int(r * 255), int(g * 255), int(b * 255)]


def make_color_map(btypes):
    unique = sorted(set(btypes))
    return {bt: BTYPE_COLORS[i % len(BTYPE_COLORS)] for i, bt in enumerate(unique)}


def simplify_btype(val):
    v = val.strip().lower()
    if "mixed" in v:   return "Mixed Use"
    if "residen" in v: return "Residential"
    if "industr" in v: return "Industrial"
    if "agri" in v:    return "Agriculture"
    return val.strip()


def poly_coords(geom):
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return [[x, y] for x, y in geom.exterior.coords]


def calc_areas(gdf_in: gpd.GeoDataFrame, area_multiplier: float) -> gpd.GeoDataFrame:
    gdf_in = gdf_in.copy()
    gdf_in["living_area"]       = 0.0
    gdf_in["commercial_area"]   = 0.0
    gdf_in["industrial_area"]   = 0.0
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


# ── Data loading (cached for the lifetime of the server process) ──────────────

@st.cache_resource
def load_data():
    bu3 = gpd.read_file(BU3_PATH, columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"])
    bu3["B_TYPE"] = bu3["B_TYPE"].apply(simplify_btype)
    bu3["cx"] = bu3.geometry.centroid.x
    bu3["cy"] = bu3.geometry.centroid.y

    bu2 = gpd.read_file(BU2_PATH, columns=["CLASSIFICATION", "LANDMARKANAMEENG", "SHAPE.STArea()", "geometry"])
    bu2 = bu2[bu2["CLASSIFICATION"] != 1].copy()
    bu2["B_TYPE"] = bu2["CLASSIFICATION"].map(
        lambda c: BU2_LABELS.get(int(c), "Unclassified") if pd.notna(c) else "Unclassified"
    )
    bu2["FLOOR_QTY"] = 1.0
    bu2["cx"] = bu2.geometry.centroid.x
    bu2["cy"] = bu2.geometry.centroid.y

    bu3 = bu3.reset_index(drop=True)
    bu2 = bu2.reset_index(drop=True)
    bu3_pts = gpd.GeoDataFrame(geometry=bu3.geometry.centroid, crs=bu3.crs)
    joined = gpd.sjoin(bu3_pts, bu2[["geometry"]], how="inner", predicate="within")
    covered = set(joined.index.unique())
    bu3_only = bu3[~bu3.index.isin(covered)].copy()
    bu3_only["LANDMARKANAMEENG"] = None

    COLS = ["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "LANDMARKANAMEENG", "cx", "cy", "geometry"]
    gdf = pd.concat([bu2[COLS], bu3_only[COLS]], ignore_index=True)
    return gpd.GeoDataFrame(gdf, geometry="geometry", crs=bu2.crs)


@st.cache_resource
def load_polygons():
    gdf = gpd.read_file(POLY_PATH)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    return gdf


def query_polygon(gdf, poly_geom, area_multiplier):
    bounds = poly_geom.bounds
    bbox = gdf.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    within = bbox[bbox.centroid.within(poly_geom)].copy()
    if within.empty:
        return within
    return calc_areas(within, area_multiplier)


def query_buildings_by_type(gdf, poly_geom, b_type):
    bounds = poly_geom.bounds
    bbox = gdf.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    within = bbox[bbox.centroid.within(poly_geom)]
    subset = within[within["B_TYPE"] == b_type].copy()
    subset["FLOOR_QTY"] = subset["FLOOR_QTY"].fillna(1).astype(int)
    subset["LANDMARKANAMEENG"] = subset["LANDMARKANAMEENG"].where(
        subset["LANDMARKANAMEENG"].notna(), other=None
    )
    return subset[["LANDMARKANAMEENG", "SHAPE.STArea()", "FLOOR_QTY", "cy", "cx"]].rename(
        columns={"LANDMARKANAMEENG": "name", "SHAPE.STArea()": "area_m2",
                 "FLOOR_QTY": "floors", "cy": "lat", "cx": "lon"}
    ).round({"area_m2": 1, "lat": 6, "lon": 6})


# ── App ───────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Building Sector Analysis", layout="wide", initial_sidebar_state="expanded")

with st.spinner("Loading building data..."):
    gdf = load_data()

with st.spinner("Loading polygons..."):
    polygons = load_polygons()

poly_lookup = {row["SCADASUBSTSHORTID"]: int(idx) for idx, row in polygons.iterrows()}

if "zones" not in st.session_state:
    st.session_state.zones = [{"lat": 35.1856, "lon": 33.3823, "name": "Zone 1", "poly_idx": None}]
if "selected_zone" not in st.session_state:
    st.session_state.selected_zone = 0
if "analysis_cache" not in st.session_state:
    st.session_state.analysis_cache = {}

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Building Sector Analysis")
    st.subheader("Parameters")
    area_multiplier = st.slider("Area multiplier", min_value=0.1, max_value=1.0, value=0.5, step=0.05)

    st.markdown("---")
    st.subheader("Substations")

    uploaded = st.file_uploader("Upload substations CSV", type="csv", help="Columns: name, lat, lon, scada_id")
    if uploaded is not None:
        file_id = (uploaded.name, uploaded.size)
        if st.session_state.get("last_upload_id") != file_id:
            df_upload = pd.read_csv(io.BytesIO(uploaded.read()))
            if {"lat", "lon"}.issubset(df_upload.columns):
                st.session_state.zones = [
                    {
                        "name": str(row.get("name", f"Zone {i+1}")),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "poly_idx": poly_lookup.get(str(row.get("scada_id", ""))),
                    }
                    for i, (_, row) in enumerate(df_upload.iterrows())
                ]
                st.session_state.analysis_cache = {}
                st.session_state.selected_zone = 0
                st.session_state.last_upload_id = file_id
                matched = sum(1 for z in st.session_state.zones if z["poly_idx"] is not None)
                st.success(f"Loaded {len(st.session_state.zones)} — {matched} matched to polygons.")
                st.rerun()
            else:
                st.error("CSV must have 'lat' and 'lon' columns.")

    zone_names = [z["name"] for z in st.session_state.zones]
    selected_idx = st.selectbox(
        "Select substation",
        options=list(range(len(st.session_state.zones))),
        format_func=lambda i: zone_names[i],
        index=st.session_state.selected_zone,
    )
    st.session_state.selected_zone = selected_idx
    zone = st.session_state.zones[selected_idx]

    st.markdown("---")
    zone["name"] = st.text_input("Name", value=zone["name"])
    zone["lat"]  = st.number_input("Latitude",  value=zone["lat"],  format="%.6f", step=0.001)
    zone["lon"]  = st.number_input("Longitude", value=zone["lon"],  format="%.6f", step=0.001)
    poly_idx = zone.get("poly_idx")
    st.caption(f"Polygon: {poly_idx}" if poly_idx is not None else "No polygon matched.")

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("+ Add"):
            n = len(st.session_state.zones) + 1
            st.session_state.zones.append({"lat": 35.1856, "lon": 33.3823, "name": f"Zone {n}", "poly_idx": None})
            st.session_state.selected_zone = len(st.session_state.zones) - 1
            st.rerun()
    with col_btn2:
        if len(st.session_state.zones) > 1 and st.button("Remove"):
            st.session_state.zones.pop(selected_idx)
            st.session_state.selected_zone = max(0, selected_idx - 1)
            st.rerun()

# ── Query selected zone ───────────────────────────────────────────────────────

n_zones = len(st.session_state.zones)

if poly_idx is not None:
    cache_key = (poly_idx, area_multiplier)
    if cache_key not in st.session_state.analysis_cache:
        with st.spinner(f"Analysing {zone['name']}..."):
            poly_geom = polygons.iloc[poly_idx].geometry
            st.session_state.analysis_cache[cache_key] = query_polygon(gdf, poly_geom, area_multiplier)
    gdf_result = st.session_state.analysis_cache[cache_key]
else:
    gdf_result = gpd.GeoDataFrame()

color_map = make_color_map(gdf_result["B_TYPE"].tolist()) if not gdf_result.empty else {}

# ── Map layers ────────────────────────────────────────────────────────────────

poly_layer_data = [
    {
        "polygon": poly_coords(polygons.iloc[z["poly_idx"]].geometry),
        "fill_color": zone_color(i, n_zones) + [60 if i == selected_idx else 20],
        "line_color": zone_color(i, n_zones) + [220],
        "line_width": 4 if i == selected_idx else 1,
    }
    for i, z in enumerate(st.session_state.zones)
    if z.get("poly_idx") is not None
]

map_df = pd.DataFrame([
    {"lon": row["cx"], "lat": row["cy"], "b_type": row["B_TYPE"],
     "color": hex_to_rgb(color_map.get(row["B_TYPE"], "#999999"))}
    for _, row in gdf_result.iterrows()
]) if not gdf_result.empty else pd.DataFrame()

pin_data = [
    {"lon": z["lon"], "lat": z["lat"], "name": z["name"], "color": zone_color(i, n_zones)}
    for i, z in enumerate(st.session_state.zones)
]

building_layer = pdk.Layer(
    "ScatterplotLayer", data=map_df,
    get_position=["lon", "lat"], get_fill_color="color",
    get_radius=8, radius_min_pixels=1, radius_max_pixels=12,
    pickable=True, opacity=0.7,
)
polygon_layer = pdk.Layer(
    "PolygonLayer", data=poly_layer_data,
    get_polygon="polygon", get_fill_color="fill_color",
    get_line_color="line_color", get_line_width="line_width",
    line_width_min_pixels=1, pickable=False, stroked=True, filled=True,
)
pin_layer = pdk.Layer(
    "ScatterplotLayer", data=pin_data,
    get_position=["lon", "lat"], get_fill_color="color",
    get_line_color=[255, 255, 255], get_radius=300,
    radius_min_pixels=4, radius_max_pixels=10,
    line_width_min_pixels=1, stroked=True, pickable=True, opacity=1.0,
)
label_layer = pdk.Layer(
    "TextLayer", data=pin_data,
    get_position=["lon", "lat"], get_text="name",
    get_size=10, get_color=[20, 20, 20],
    get_background_color=[255, 255, 255, 180], background_padding=[2, 1],
    get_anchor="middle", get_alignment_baseline="bottom",
    get_pixel_offset=[0, -10], pickable=False,
)

avg_lat = sum(z["lat"] for z in st.session_state.zones) / n_zones
avg_lon = sum(z["lon"] for z in st.session_state.zones) / n_zones
lat_span = max(z["lat"] for z in st.session_state.zones) - min(z["lat"] for z in st.session_state.zones)
lon_span = max(z["lon"] for z in st.session_state.zones) - min(z["lon"] for z in st.session_state.zones)
span_deg = max(lat_span, lon_span, 0.01)
zoom = max(min(math.log2(360 / span_deg) - 1, 14), 4)
view = pdk.ViewState(latitude=avg_lat, longitude=avg_lon, zoom=zoom, pitch=0)
deck = pdk.Deck(
    layers=[polygon_layer, building_layer, pin_layer, label_layer],
    initial_view_state=view,
    tooltip={"html": "<b>{name}</b>{b_type}"},
    map_style="road",
)
st.pydeck_chart(deck, use_container_width=True, height=650)

# ── Analytics ─────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader(zone["name"])

if not gdf_result.empty:
    # Clear type selection when polygon changes
    if st.session_state.get("detail_poly") != poly_idx:
        st.session_state.pop("selected_btype", None)
        st.session_state["detail_poly"] = poly_idx

    col_m1, col_m2, col_pie, col_a, col_b = st.columns([1, 1, 1.5, 1, 1])
    with col_m1:
        st.metric("Buildings", f"{len(gdf_result):,}")
        st.metric("Estimated residents", f"{int(gdf_result['residents'].sum()):,}")
        st.metric("Residential area", f"{gdf_result['living_area'].sum():,.0f} m²")
    with col_m2:
        st.metric("Commercial area", f"{gdf_result['commercial_area'].sum():,.0f} m²")
        st.metric("Industrial area", f"{gdf_result['industrial_area'].sum():,.0f} m²")
        st.metric("Agricultural area", f"{gdf_result['agricultural_area'].sum():,.0f} m²")
    with col_pie:
        type_counts = gdf_result["B_TYPE"].value_counts().reset_index()
        type_counts.columns = ["Type", "Count"]
        type_counts["Type"] = type_counts["Type"].map(lambda t: PIE_GROUPS.get(t, t))
        type_counts = type_counts.groupby("Type", as_index=False)["Count"].sum()
        pie_domain = sorted(type_counts["Type"].unique())
        pie_range = [color_map.get(t, BTYPE_COLORS[i % len(BTYPE_COLORS)]) for i, t in enumerate(pie_domain)]
        pie = (
            alt.Chart(type_counts)
            .mark_arc(innerRadius=40)
            .encode(
                theta=alt.Theta("Count:Q"),
                color=alt.Color("Type:N", scale=alt.Scale(domain=pie_domain, range=pie_range),
                                legend=alt.Legend(title="B_TYPE")),
                tooltip=["Type:N", "Count:Q"],
            )
            .properties(height=300)
        )
        st.altair_chart(pie, use_container_width=True)
    with col_a:
        st.markdown("**Count per B_TYPE** — click to inspect")
        tc = gdf_result["B_TYPE"].value_counts().reset_index()
        tc.columns = ["Type", "Count"]
        tc_clickable = tc[tc["Type"] != "Residential"].reset_index(drop=True)
        sel = st.dataframe(tc_clickable, use_container_width=True, hide_index=True,
                           on_select="rerun", selection_mode="single-row")
        if sel.selection.rows:
            st.session_state.selected_btype = tc_clickable.iloc[sel.selection.rows[0]]["Type"]
    with col_b:
        st.markdown("**Living area per B_TYPE (m²)**")
        abt = gdf_result.groupby("B_TYPE")["living_area"].sum().reset_index()
        abt.columns = ["Type", "Living Area (m²)"]
        abt["Living Area (m²)"] = abt["Living Area (m²)"].round(1)
        abt = abt.sort_values("Living Area (m²)", ascending=False)
        st.dataframe(abt, use_container_width=True, hide_index=True)

    # Individual building detail — fetched only on row click
    selected_btype = st.session_state.get("selected_btype")
    if selected_btype:
        detail_key = ("btype", poly_idx, area_multiplier, selected_btype)
        if detail_key not in st.session_state.analysis_cache:
            with st.spinner(f"Loading {selected_btype} buildings..."):
                poly_geom = polygons.iloc[poly_idx].geometry
                st.session_state.analysis_cache[detail_key] = query_buildings_by_type(
                    gdf, poly_geom, selected_btype
                )
        detail_df = st.session_state.analysis_cache.get(detail_key)
        if detail_df is not None and not detail_df.empty:
            with st.expander(f"{selected_btype} — {len(detail_df)} buildings", expanded=True):
                st.dataframe(detail_df, use_container_width=True, hide_index=True)
else:
    st.info("No buildings in this polygon." if poly_idx is not None else "No polygon matched for this zone.")