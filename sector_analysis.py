import math
import colorsys
import io
import requests
import geopandas as gpd
import pandas as pd
import pydeck as pdk
import streamlit as st
import altair as alt

GPKG_PATH  = r"C:\Users\chris\iCloudDrive\KIOS\failed\cyprus_BU3_full.gpkg"
POLY_PATH  = r"C:\Users\chris\iCloudDrive\KIOS\DistrTrSubstThPoly.geojson"

BTYPE_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8",
    "#f58231", "#911eb4", "#42d4f4", "#f032e6",
    "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3",
]


def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]


def zone_color(i, n):
    hue = i / max(n, 1)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
    return [int(r * 255), int(g * 255), int(b * 255)]


def simplify_btype(val):
    v = val.strip().lower()
    if "mixed" in v:   return "Mixed Use"
    if "residen" in v: return "Residential"
    if "industr" in v: return "Industrial"
    if "agri" in v:    return "Agriculture"
    return val.strip()


@st.cache_data
def load_data():
    gdf = gpd.read_file(GPKG_PATH, columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"])
    gdf["B_TYPE"] = gdf["B_TYPE"].apply(simplify_btype)
    gdf["cx"] = gdf.geometry.centroid.x
    gdf["cy"] = gdf.geometry.centroid.y
    return gdf


@st.cache_data
def load_polygons():
    gdf = gpd.read_file(POLY_PATH)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    return gdf


def make_color_map(btypes):
    unique = sorted(btypes.unique())
    return {bt: BTYPE_COLORS[i % len(BTYPE_COLORS)] for i, bt in enumerate(unique)}


def calc_areas(gdf_in, floor_multiplier):
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


def query_polygon(gdf_buildings, poly_geom, floor_multiplier):
    bounds = poly_geom.bounds
    bbox = gdf_buildings.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    within = bbox[bbox.centroid.within(poly_geom)].copy()
    if within.empty:
        return within
    return calc_areas(within, floor_multiplier)


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def poly_coords(geom):
    """Return exterior ring [[lon,lat],...] for a Polygon or first part of MultiPolygon."""
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return [[x, y] for x, y in geom.exterior.coords]


# ── App ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Building Sector Analysis", layout="wide", initial_sidebar_state="expanded")

with st.spinner("Loading building data..."):
    gdf = load_data()

with st.spinner("Loading polygons..."):
    polygons = load_polygons()

if "zones" not in st.session_state:
    st.session_state.zones = [{"lat": 35.1856, "lon": 33.3823, "name": "Zone 1", "poly_idx": None}]
if "selected_zone" not in st.session_state:
    st.session_state.selected_zone = 0
if "zone_cache" not in st.session_state:
    st.session_state.zone_cache = {}

with st.sidebar:
    st.title("Building Sector Analysis")
    st.subheader("Parameters")
    floor_multiplier = st.slider("Floor multiplier", min_value=0.1, max_value=1.0, value=0.5, step=0.05)

    st.markdown("---")
    st.subheader("Substations")

    uploaded = st.file_uploader("Upload substations CSV", type="csv", help="Columns: name, lat, lon, scada_id")
    if uploaded is not None:
        file_id = (uploaded.name, uploaded.size)
        if st.session_state.get("last_upload_id") != file_id:
            df_upload = pd.read_csv(io.BytesIO(uploaded.read()))
            if {"lat", "lon"}.issubset(df_upload.columns):
                poly_lookup = {row["SCADASUBSTSHORTID"]: idx for idx, row in polygons.iterrows()}
                st.session_state.zones = [
                    {
                        "name": str(row.get("name", f"Zone {i+1}")),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "poly_idx": poly_lookup.get(str(row.get("scada_id", ""))),
                    }
                    for i, (_, row) in enumerate(df_upload.iterrows())
                ]
                st.session_state.zone_cache = {}
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
    if poly_idx is not None:
        st.caption(f"Polygon: {polygons.iloc[poly_idx].get('SCADASUBSTSHORTID', '')}")
    else:
        st.caption("No polygon matched.")

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("+ Add"):
            n = len(st.session_state.zones) + 1
            st.session_state.zones.append({"lat": 35.1856, "lon": 33.3823, "name": f"Zone {n}", "poly_idx": None})
            st.session_state.selected_zone = len(st.session_state.zones) - 1
            st.rerun()
    with col_btn2:
        if len(st.session_state.zones) > 1:
            if st.button("Remove"):
                st.session_state.zones.pop(selected_idx)
                st.session_state.selected_zone = max(0, selected_idx - 1)
                st.rerun()

# ── Query selected zone ───────────────────────────────────────────────────────

n_zones = len(st.session_state.zones)
poly_idx = zone.get("poly_idx")

if poly_idx is not None:
    cache_key = ("poly", poly_idx, floor_multiplier)
    if cache_key not in st.session_state.zone_cache:
        with st.spinner(f"Analysing {zone['name']}..."):
            poly_geom = polygons.iloc[poly_idx].geometry
            st.session_state.zone_cache[cache_key] = query_polygon(gdf, poly_geom, floor_multiplier)
    gdf_result = st.session_state.zone_cache[cache_key]
else:
    gdf_result = gpd.GeoDataFrame()

color_map = make_color_map(gdf_result["B_TYPE"]) if not gdf_result.empty else {}

# ── Map layers ────────────────────────────────────────────────────────────────

# All polygon outlines
poly_layer_data = []
for i, z in enumerate(st.session_state.zones):
    pidx = z.get("poly_idx")
    if pidx is None:
        continue
    geom = polygons.iloc[pidx].geometry
    c = zone_color(i, n_zones)
    poly_layer_data.append({
        "polygon": poly_coords(geom),
        "fill_color": c + [60 if i == selected_idx else 20],
        "line_color": c + [220],
        "line_width": 4 if i == selected_idx else 1,
    })

if not gdf_result.empty:
    map_df = gdf_result[["cx", "cy", "B_TYPE"]].copy().reset_index(drop=True)
    map_df["color"] = map_df["B_TYPE"].map(lambda t: hex_to_rgb(color_map.get(t, "#999999")))
else:
    map_df = pd.DataFrame({"cx": [], "cy": [], "B_TYPE": [], "color": []})

pin_data = [
    {"lon": z["lon"], "lat": z["lat"], "name": z["name"], "color": zone_color(i, n_zones)}
    for i, z in enumerate(st.session_state.zones)
]

building_layer = pdk.Layer(
    "ScatterplotLayer",
    data=map_df,
    get_position=["cx", "cy"],
    get_fill_color="color",
    get_radius=8,
    radius_min_pixels=1,
    radius_max_pixels=12,
    pickable=True,
    opacity=0.7,
)
polygon_layer = pdk.Layer(
    "PolygonLayer",
    data=poly_layer_data,
    get_polygon="polygon",
    get_fill_color="fill_color",
    get_line_color="line_color",
    get_line_width="line_width",
    line_width_min_pixels=1,
    pickable=False,
    stroked=True,
    filled=True,
)
pin_layer = pdk.Layer(
    "ScatterplotLayer",
    data=pin_data,
    get_position=["lon", "lat"],
    get_fill_color="color",
    get_line_color=[255, 255, 255],
    get_radius=300,
    radius_min_pixels=4,
    radius_max_pixels=10,
    line_width_min_pixels=1,
    stroked=True,
    pickable=True,
    opacity=1.0,
)
label_layer = pdk.Layer(
    "TextLayer",
    data=pin_data,
    get_position=["lon", "lat"],
    get_text="name",
    get_size=10,
    get_color=[20, 20, 20],
    get_background_color=[255, 255, 255, 180],
    background_padding=[2, 1],
    get_anchor="middle",
    get_alignment_baseline="bottom",
    get_pixel_offset=[0, -10],
    pickable=False,
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
    tooltip={"html": "<b>{name}</b>{B_TYPE}"},
    map_style="road",
)
st.pydeck_chart(deck, use_container_width=True, height=650)

# ── Analytics below map ───────────────────────────────────────────────────────

st.markdown("---")
st.subheader(f"{zone['name']}")

if not gdf_result.empty:
    col_metrics, col_pie, col_a, col_b = st.columns([1, 1.5, 1, 1])
    with col_metrics:
        st.metric("Buildings", f"{len(gdf_result):,}")
        st.metric("Estimated residents", f"{gdf_result['residents'].sum():,}")
        st.metric("Residential area", f"{gdf_result['living_area'].sum():,.0f} m²")
        st.metric("Commercial area", f"{gdf_result['commercial_area'].sum():,.0f} m²")
        st.metric("Industrial area", f"{gdf_result['industrial_area'].sum():,.0f} m²")
    with col_pie:
        type_counts = gdf_result["B_TYPE"].value_counts().reset_index()
        type_counts.columns = ["Type", "Count"]
        domain = list(color_map.keys())
        range_ = [color_map[k] for k in domain]
        pie = (
            alt.Chart(type_counts)
            .mark_arc(innerRadius=40)
            .encode(
                theta=alt.Theta("Count:Q"),
                color=alt.Color("Type:N", scale=alt.Scale(domain=domain, range=range_), legend=alt.Legend(title="B_TYPE")),
                tooltip=["Type:N", "Count:Q"],
            )
            .properties(height=300)
        )
        st.altair_chart(pie, use_container_width=True)
    with col_a:
        st.markdown("**Count per B_TYPE**")
        tc = gdf_result["B_TYPE"].value_counts().reset_index()
        tc.columns = ["Type", "Count"]
        st.dataframe(tc, use_container_width=True, hide_index=True)
    with col_b:
        st.markdown("**Living area per B_TYPE (m²)**")
        abt = gdf_result.groupby("B_TYPE")["living_area"].sum().reset_index()
        abt.columns = ["Type", "Living Area (m²)"]
        abt["Living Area (m²)"] = abt["Living Area (m²)"].round(1)
        abt = abt.sort_values("Living Area (m²)", ascending=False)
        st.dataframe(abt, use_container_width=True, hide_index=True)
else:
    st.info("No buildings in this polygon.")