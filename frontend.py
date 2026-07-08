import math
import colorsys
import io

import requests as http
import pandas as pd
import pydeck as pdk
import streamlit as st
import altair as alt
from shapely.geometry import Point, Polygon

API = "http://localhost:8000"

BTYPE_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8",
    "#f58231", "#911eb4", "#42d4f4", "#f032e6",
    "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3",
]

# B_TYPE → sector for chart/table grouping. Unmapped labels fall into "Services".
SECTOR_GROUPS = {
    "Residential":                    "Residential",
    "Villa / holiday home":           "Residential",
    "Mixed Use":                      "Residential",
    "Nursery / kindergarten":         "Education",
    "Elementary school":              "Education",
    "Secondary school":               "Education",
    "Higher education":               "Education",
    "Hotel / apartment building":     "Hotels and restaurants",
    "Tourist village / resort":       "Hotels and restaurants",
    "Shopping mall":                  "Trade",
    "Health centre / hospital":       "Health",
    "Bank":                           "Private offices",
    "Broadcast station":              "Private offices",
    "Community / government":         "Public administration",
    "Town hall":                      "Public administration",
    "Public utility office":          "Public administration",
    "Police station":                 "Public administration",
    "Fire station":                   "Public administration",
    "Post office":                    "Public administration",
    "Electricity substation":         "Gas and water supply",
    "Petroleum storage / refinery":   "Gas and water supply",
    "Industrial":                     "Manufacturing",
    "Factory / industrial plant":     "Manufacturing",
    "Agriculture":                    "Agriculture",
    # Everything else (cultural, religious, sports, heritage, unclassified) → Services
}

def sector_of(btype):
    return SECTOR_GROUPS.get(btype, "Services")


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


def normalize_subs_df(df):
    """Accept common column variants: long/longitude/lng for lon, latitude for lat."""
    df = df.rename(columns=lambda c: str(c).strip().lower())
    return df.rename(columns={"long": "lon", "longitude": "lon", "lng": "lon", "latitude": "lat"})


def zone_name_from_row(row, i):
    for key in ("name", "scada_id"):
        v = row.get(key)
        if pd.notna(v) and str(v).strip():
            return str(v).strip()
    return f"Zone {i+1}"


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@st.cache_data
def fetch_polygons():
    return http.get(f"{API}/polygons").json()


@st.cache_data
def fetch_polygon_lookup():
    return http.get(f"{API}/polygon_lookup").json()


# ── App ───────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Building Sector Analysis", layout="wide", initial_sidebar_state="expanded")

try:
    all_polygons = fetch_polygons()
    poly_lookup  = fetch_polygon_lookup()
except Exception:
    st.error("Cannot reach backend. Start it with: `py -3.14 -m uvicorn backend:app`")
    st.stop()

if "zones" not in st.session_state:
    st.session_state.zones = [{"lat": 35.1856, "lon": 33.3823, "name": "Zone 1", "poly_idx": None}]
if "selected_zone" not in st.session_state:
    st.session_state.selected_zone = 0
if "analysis_cache" not in st.session_state:
    st.session_state.analysis_cache = {}

# ── Map click → select that substation ────────────────────────────────────────
# The pydeck chart (key="deck") reruns the script on click; its selection state
# is read here, before the sidebar renders, so the dropdown follows the click.

event = st.session_state.get("deck")
if event is not None and getattr(event, "selection", None) is not None:
    objs = event.selection.get("objects", {}) if hasattr(event.selection, "get") else event.selection.objects
    picked = None
    for layer_id in ("pins", "polys"):
        if objs.get(layer_id):
            picked = objs[layer_id][0]
            break
    if picked is not None:
        sig = str(event.selection.indices)
        if st.session_state.get("last_map_pick") != sig:
            st.session_state.last_map_pick = sig
            zi = picked.get("zone_idx")
            if zi is not None and 0 <= int(zi) < len(st.session_state.zones):
                st.session_state.selected_zone = int(zi)

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
            df_upload = normalize_subs_df(pd.read_csv(io.BytesIO(uploaded.read())))
            if {"lat", "lon"}.issubset(df_upload.columns):
                df_upload = df_upload.dropna(subset=["lat", "lon"]).reset_index(drop=True)
                shapely_polys = [(p["idx"], Polygon(p["polygon"])) for p in all_polygons]

                def find_poly_idx(lon, lat):
                    pt = Point(lon, lat)
                    for idx, poly in shapely_polys:
                        if poly.contains(pt):
                            return idx
                    return None

                st.session_state.zones = [
                    {
                        "name": zone_name_from_row(row, i),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        # Match by scada_id; fall back to the polygon the station sits inside
                        "poly_idx": poly_lookup.get(str(row.get("scada_id", "")),
                                                    find_poly_idx(float(row["lon"]), float(row["lat"]))),
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

# ── Fetch analysis from backend ───────────────────────────────────────────────

n_zones = len(st.session_state.zones)
result = None

if poly_idx is not None:
    cache_key = (poly_idx, area_multiplier)
    if cache_key not in st.session_state.analysis_cache:
        with st.spinner(f"Analysing {zone['name']}..."):
            resp = http.post(f"{API}/analyse", json={"poly_idx": poly_idx, "area_multiplier": area_multiplier})
            st.session_state.analysis_cache[cache_key] = resp.json()
    result = st.session_state.analysis_cache[cache_key]

# ── Map ───────────────────────────────────────────────────────────────────────

poly_layer_data = [
    {
        "polygon": p["polygon"],
        "fill_color": zone_color(i, n_zones) + [60 if i == selected_idx else 20],
        "line_color": zone_color(i, n_zones) + [220],
        "line_width": 4 if i == selected_idx else 1,
        "name": z["name"],
        "zone_idx": i,
    }
    for i, z in enumerate(st.session_state.zones)
    if z.get("poly_idx") is not None
    for p in [next((x for x in all_polygons if x["idx"] == z["poly_idx"]), None)]
    if p is not None
]

buildings = result["buildings"] if result else []
color_map = make_color_map([b["b_type"] for b in buildings]) if buildings else {}
map_df = pd.DataFrame([
    {"lon": b["lon"], "lat": b["lat"], "b_type": b["b_type"],
     "color": hex_to_rgb(color_map.get(b["b_type"], "#999999"))}
    for b in buildings
])

pin_data = [
    {"lon": z["lon"], "lat": z["lat"], "name": z["name"], "color": zone_color(i, n_zones), "zone_idx": i}
    for i, z in enumerate(st.session_state.zones)
]

building_layer = pdk.Layer(
    "ScatterplotLayer", data=map_df,
    get_position=["lon", "lat"], get_fill_color="color",
    get_radius=8, radius_min_pixels=1, radius_max_pixels=12,
    pickable=True, opacity=0.7,
)
polygon_layer = pdk.Layer(
    "PolygonLayer", data=poly_layer_data, id="polys",
    get_polygon="polygon", get_fill_color="fill_color",
    get_line_color="line_color", get_line_width="line_width",
    line_width_min_pixels=1, pickable=True, stroked=True, filled=True,
)
pin_layer = pdk.Layer(
    "ScatterplotLayer", data=pin_data, id="pins",
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
st.pydeck_chart(deck, use_container_width=True, height=650,
                on_select="rerun", selection_mode="single-object", key="deck")

# ── Analytics ─────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader(zone["name"])

if result and result["building_count"] > 0:
    # Clear type selection when polygon changes
    if st.session_state.get("detail_poly") != poly_idx:
        st.session_state.pop("selected_btype", None)
        st.session_state["detail_poly"] = poly_idx

    col_m1, col_m2, col_pie, col_a, col_b = st.columns([1, 1, 1.5, 1, 1])
    with col_m1:
        st.metric("Buildings", f"{result['building_count']:,}")
        st.metric("Estimated residents", f"{result['residents']:,}")
        st.metric("Residential area", f"{result['living_area']:,.0f} m²")
    with col_m2:
        st.metric("Commercial area", f"{result['commercial_area']:,.0f} m²")
        st.metric("Industrial area", f"{result['industrial_area']:,.0f} m²")
        st.metric("Agricultural area", f"{result['agricultural_area']:,.0f} m²")
    with col_pie:
        by_type = result["by_type"]
        pie_df = pd.DataFrame(by_type).rename(columns={"type": "Type", "count": "Count"})
        pie_df["Type"] = pie_df["Type"].map(sector_of)
        pie_df = pie_df.groupby("Type", as_index=False)["Count"].sum()
        pie_domain = sorted(pie_df["Type"].unique())
        pie_range = [color_map.get(t, BTYPE_COLORS[i % len(BTYPE_COLORS)]) for i, t in enumerate(pie_domain)]
        pie = (
            alt.Chart(pie_df)
            .mark_arc(innerRadius=40)
            .encode(
                theta=alt.Theta("Count:Q"),
                color=alt.Color("Type:N", scale=alt.Scale(domain=pie_domain, range=pie_range), legend=alt.Legend(title="B_TYPE")),
                tooltip=["Type:N", "Count:Q"],
            )
            .properties(height=300)
        )
        st.altair_chart(pie, use_container_width=True)
    with col_a:
        st.markdown("**Count per B_TYPE** — click to inspect")
        tc = pd.DataFrame(by_type).rename(columns={"type": "Type", "count": "Count"})[["Type", "Count"]]
        tc_clickable = tc[tc["Type"] != "Residential"].reset_index(drop=True)
        sel = st.dataframe(tc_clickable, use_container_width=True, hide_index=True,
                           on_select="rerun", selection_mode="single-row")
        if sel.selection.rows:
            st.session_state.selected_btype = tc_clickable.iloc[sel.selection.rows[0]]["Type"]
    with col_b:
        st.markdown("**Living area per B_TYPE (m²)**")
        abt = pd.DataFrame(by_type).rename(columns={"type": "Type", "living_area": "Living Area (m²)"})[["Type", "Living Area (m²)"]]
        abt = abt.sort_values("Living Area (m²)", ascending=False)
        st.dataframe(abt, use_container_width=True, hide_index=True)

    # Individual building detail — fetched only on row click
    selected_btype = st.session_state.get("selected_btype")
    if selected_btype:
        detail_key = ("btype", poly_idx, selected_btype)
        if detail_key not in st.session_state.analysis_cache:
            with st.spinner(f"Loading {selected_btype} buildings..."):
                try:
                    resp = http.post(f"{API}/buildings", json={"poly_idx": poly_idx, "b_type": selected_btype})
                    resp.raise_for_status()
                    st.session_state.analysis_cache[detail_key] = resp.json()
                except Exception as e:
                    st.error(f"Failed to load buildings: {e}")
                    st.session_state.pop("selected_btype", None)
                    st.stop()
        detail = st.session_state.analysis_cache.get(detail_key)
        if detail:
            with st.expander(f"{selected_btype} — {detail['count']} buildings", expanded=True):
                df_detail = pd.DataFrame(detail["buildings"])
                st.dataframe(df_detail, use_container_width=True, hide_index=True)
else:
    st.info("No buildings in this polygon." if poly_idx is not None else "No polygon matched for this zone.")