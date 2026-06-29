import requests
import geopandas as gpd
import pydeck as pdk
import streamlit as st
import altair as alt
from shapely.geometry import Point

GPKG_PATH = r"C:\Users\chris\iCloudDrive\KIOS\failed\cyprus_BU3_full.gpkg"
BTYPE_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8",
    "#f58231", "#911eb4", "#42d4f4", "#f032e6",
    "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3",
]


def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]


def simplify_btype(val):
    v = val.strip().lower()
    if "mixed" in v:
        return "Mixed Use"
    if "residen" in v:
        return "Residential"
    if "industr" in v:
        return "Industrial"
    if "agri" in v:
        return "Agriculture"
    return val.strip()


@st.cache_data
def load_data():
    gdf = gpd.read_file(
        GPKG_PATH,
        columns=["B_TYPE", "FLOOR_QTY", "SHAPE.STArea()", "geometry"],
    )
    gdf["B_TYPE"] = gdf["B_TYPE"].apply(simplify_btype)
    gdf["cx"] = gdf.geometry.centroid.x
    gdf["cy"] = gdf.geometry.centroid.y
    return gdf


def make_color_map(btypes):
    unique = sorted(btypes.unique())
    return {bt: BTYPE_COLORS[i % len(BTYPE_COLORS)] for i, bt in enumerate(unique)}


st.set_page_config(page_title="Building Sector Analysis", layout="wide")
st.title("Building Sector Analysis")

with st.spinner("Loading building data..."):
    gdf = load_data()

if "lat" not in st.session_state:
    st.session_state.lat = 35.1856
if "lon" not in st.session_state:
    st.session_state.lon = 33.3823

col_input, col_map, col_pie = st.columns([1, 2, 1.5])

with col_input:
    st.subheader("Parameters")
    with st.form("search_form", clear_on_submit=True):
        search = st.text_input("Search location", placeholder="e.g. Limassol, Larnaca...")
        searched = st.form_submit_button("Go")
    if searched and search:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": search, "format": "json", "limit": 1, "countrycodes": "cy"},
            headers={"User-Agent": "building-sector-analysis"},
            timeout=10,
        )
        results = resp.json()
        if results:
            st.session_state.lat = float(results[0]["lat"])
            st.session_state.lon = float(results[0]["lon"])
            st.rerun()
        else:
            st.warning("Location not found.")
    lat = st.number_input("Latitude", value=st.session_state.lat, format="%.6f", step=0.001)
    lon = st.number_input("Longitude", value=st.session_state.lon, format="%.6f", step=0.001)
    radius_km = st.slider("Radius (km)", min_value=0.5, max_value=250.0, value=1.0, step=0.5)
    radius_m = radius_km * 1000
    floor_multiplier = st.slider("Floor multiplier", min_value=0.1, max_value=1.0, value=0.5, step=0.05)

center = Point(lon, lat)
buffer_deg = radius_m / 111320
bbox = gdf.cx[lon - buffer_deg : lon + buffer_deg, lat - buffer_deg : lat + buffer_deg]
distances = bbox.centroid.distance(center) * 111320
mask = distances <= radius_m
gdf_circle = bbox[mask].copy()

if not gdf_circle.empty:
    gdf_circle["living_area"] = 0.0
    gdf_circle["commercial_area"] = 0.0
    res_mask = gdf_circle["B_TYPE"] == "Residential"
    mixed_mask = gdf_circle["B_TYPE"] == "Mixed Use"
    gdf_circle.loc[res_mask, "living_area"] = (
        gdf_circle.loc[res_mask, "FLOOR_QTY"] * floor_multiplier * gdf_circle.loc[res_mask, "SHAPE.STArea()"]
    )
    mixed_total_area = (
        gdf_circle.loc[mixed_mask, "FLOOR_QTY"] * floor_multiplier * gdf_circle.loc[mixed_mask, "SHAPE.STArea()"]
    )
    gdf_circle.loc[mixed_mask, "living_area"] = mixed_total_area * 0.5
    gdf_circle.loc[mixed_mask, "commercial_area"] = mixed_total_area * 0.5
    gdf_circle["residents"] = (gdf_circle["living_area"] / 75).round(0).astype(int)
    gdf_circle["industrial_area"] = 0.0
    industrial_mask = gdf_circle["B_TYPE"] == "Industrial"
    gdf_circle.loc[industrial_mask, "industrial_area"] = (
        gdf_circle.loc[industrial_mask, "FLOOR_QTY"] * gdf_circle.loc[industrial_mask, "SHAPE.STArea()"]
    )

color_map = make_color_map(gdf_circle["B_TYPE"]) if not gdf_circle.empty else {}

with col_map:
    if not gdf_circle.empty:
        map_df = gdf_circle[["cx", "cy", "B_TYPE"]].copy().reset_index(drop=True)
        map_df["color"] = map_df["B_TYPE"].map(lambda t: hex_to_rgb(color_map.get(t, "#999999")))
    else:
        map_df = gpd.GeoDataFrame({"cx": [], "cy": [], "B_TYPE": [], "color": []})

    scatter = pdk.Layer(
        "ScatterplotLayer",
        data=map_df,
        get_position=["cx", "cy"],
        get_fill_color="color",
        get_radius=max(radius_m / 100, 5),
        pickable=True,
        opacity=0.7,
    )

    circle_points = []
    import math
    for i in range(64):
        angle = 2 * math.pi * i / 64
        clat = lat + (radius_m / 111320) * math.cos(angle)
        clon = lon + (radius_m / (111320 * math.cos(math.radians(lat)))) * math.sin(angle)
        circle_points.append([clon, clat])
    circle_points.append(circle_points[0])

    circle_layer = pdk.Layer(
        "PathLayer",
        data=[{"path": circle_points}],
        get_path="path",
        get_color=[0, 0, 0, 180],
        width_min_pixels=2,
    )

    zoom = max(15 - math.log2(max(radius_m, 100) / 500), 8)

    view = pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom, pitch=0)
    deck = pdk.Deck(
        layers=[scatter, circle_layer],
        initial_view_state=view,
        tooltip={"text": "{B_TYPE}"},
        map_style="road",
    )
    st.pydeck_chart(deck, use_container_width=True, height=500)

with col_pie:
    st.markdown("**B_TYPE distribution**")
    if not gdf_circle.empty:
        type_counts = gdf_circle["B_TYPE"].value_counts().reset_index()
        type_counts.columns = ["Type", "Count"]
        domain = list(color_map.keys())
        range_ = [color_map[k] for k in domain]
        pie = (
            alt.Chart(type_counts)
            .mark_arc(innerRadius=50)
            .encode(
                theta=alt.Theta("Count:Q"),
                color=alt.Color(
                    "Type:N",
                    scale=alt.Scale(domain=domain, range=range_),
                    legend=alt.Legend(title="B_TYPE"),
                ),
                tooltip=["Type:N", "Count:Q"],
            )
            .properties(height=400)
        )
        st.altair_chart(pie, use_container_width=True)
    else:
        st.info("No buildings in this area.")

st.markdown("---")
st.subheader(f"Results: {len(gdf_circle)} buildings within {radius_m}m")

if not gdf_circle.empty:
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**Building count per B_TYPE**")
        type_counts = gdf_circle["B_TYPE"].value_counts().reset_index()
        type_counts.columns = ["Type", "Count"]
        st.dataframe(type_counts, use_container_width=True, hide_index=True)

    with col_b:
        st.markdown("**Total living area per B_TYPE (m²)**")
        area_by_type = gdf_circle.groupby("B_TYPE")["living_area"].sum().reset_index()
        area_by_type.columns = ["Type", "Living Area (m²)"]
        area_by_type["Living Area (m²)"] = area_by_type["Living Area (m²)"].round(1)
        area_by_type = area_by_type.sort_values("Living Area (m²)", ascending=False)
        st.dataframe(area_by_type, use_container_width=True, hide_index=True)

    total_area = gdf_circle["living_area"].sum()
    total_residents = gdf_circle["residents"].sum()
    total_commercial = gdf_circle["commercial_area"].sum()
    total_industrial = gdf_circle["industrial_area"].sum()
    st.markdown(f"**Total residential living area: {total_area:,.1f} m²**")
    st.markdown(f"**Estimated residents: {total_residents:,}** (living area ÷ 75 m²/person)")
    st.markdown(f"**Total commercial area (from mixed use): {total_commercial:,.1f} m²**")
    st.markdown(f"**Total industrial floor area: {total_industrial:,.1f} m²**")
    st.bar_chart(area_by_type.set_index("Type"))
else:
    st.info("No buildings in this area.")