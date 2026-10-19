"""Local stand-in for the Databricks dashboard.

Fetches live OpenSky data and applies the same Bronze -> Silver -> Gold logic
as transform_silver.py / aggregate_gold.py, using pandas instead of Spark.
Extras: region focus, filters, callsign search, airline breakdown,
snapshot history (SQLite), auto-refresh and CSV export.
"""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="SkyStream Lakehouse Dashboard", page_icon="✈️", layout="wide")
DB = Path(__file__).with_name("snapshots.db")
LOW_ALT_M = 500
COLS = ["icao24", "callsign", "origin_country", "time_position", "last_contact",
        "longitude", "latitude", "baro_altitude", "on_ground", "velocity"]

# name: (lat_min, lat_max, lon_min, lon_max, lat, lon, zoom)
REGIONS = {
    "World": (-90, 90, -180, 180, 30, 10, 1.2),
    "Pakistan airspace": (23.5, 37.1, 60.8, 77.9, 30, 69, 4.2),
    "Middle East": (12, 42, 34, 63, 27, 48, 3.6),
    "Europe": (35, 72, -12, 40, 52, 12, 3.2),
    "North America": (15, 72, -170, -50, 42, -97, 2.4),
}


# ---------- Bronze ----------
@st.cache_data(ttl=60)
def fetch_bronze() -> tuple[pd.DataFrame, str]:
    r = requests.get("https://opensky-network.org/api/states/all", timeout=30)
    r.raise_for_status()
    rows = [s[:10] for s in (r.json().get("states") or [])]
    df = pd.DataFrame(rows, columns=COLS).rename(columns={"baro_altitude": "altitude"})
    df = df.drop(columns=["time_position", "last_contact"])
    return df, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------- Silver ----------
def to_silver(bronze: pd.DataFrame) -> pd.DataFrame:
    s = bronze.dropna(subset=["callsign"]).copy()
    s["callsign"] = s["callsign"].str.strip()
    s = s[s["callsign"] != ""].drop_duplicates("icao24")
    s["airline"] = s["callsign"].str.extract(r"^([A-Z]{3})\d", expand=False)
    s["altitude_ft"] = (s["altitude"] * 3.28084).round()
    s["speed_kmh"] = (s["velocity"] * 3.6).round()
    return s


# ---------- history ----------
def save_snapshot(ts: str, silver: pd.DataFrame) -> None:
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS snap(ts TEXT, country TEXT, total INT, in_air INT, low_alt INT)")
    if con.execute("SELECT 1 FROM snap WHERE ts=? LIMIT 1", (ts,)).fetchone() is None:
        g = silver.assign(air=~silver["on_ground"].astype(bool),
                          low=(silver["altitude"] < LOW_ALT_M) & ~silver["on_ground"].astype(bool))
        g = g.groupby("origin_country").agg(total=("icao24", "count"), in_air=("air", "sum"), low_alt=("low", "sum"))
        con.executemany("INSERT INTO snap VALUES (?,?,?,?,?)",
                        [(ts, c, int(r.total), int(r.in_air), int(r.low_alt)) for c, r in g.iterrows()])
        con.commit()
    con.close()


def load_history() -> pd.DataFrame:
    if not DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB)
    try:
        return pd.read_sql("SELECT * FROM snap", con)
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()


# ---------- sidebar ----------
st.title("✈️ SkyStream Lakehouse: Real-Time Flight Dashboard")
sb = st.sidebar
sb.header("Controls")
if sb.button("🔄 Refresh now"):
    fetch_bronze.clear()
auto = sb.checkbox("Auto-refresh every 60 s")

try:
    bronze, fetched_at = fetch_bronze()
except Exception as e:
    st.error(f"OpenSky API request failed (rate limit or network): {e}")
    st.stop()

silver_all = to_silver(bronze)
save_snapshot(fetched_at, silver_all)
sb.caption(f"Last fetch: {fetched_at}")

sb.header("Filters")
region = sb.selectbox("Region", list(REGIONS))
la0, la1, lo0, lo1, vlat, vlon, vzoom = REGIONS[region]
status_f = sb.radio("Status", ["All", "In air", "On ground"], horizontal=True)
countries = sorted(silver_all["origin_country"].dropna().unique())
picked = sb.multiselect("Origin country (registration)", countries)
airlines = sorted(silver_all["airline"].dropna().unique())
picked_air = sb.multiselect("Airline code (callsign prefix)", airlines)
search = sb.text_input("Search callsign / ICAO24")
alt_rng = sb.slider("Altitude (m)", 0, 15000, (0, 15000), step=100)
spd_rng = sb.slider("Speed (km/h)", 0, 1200, (0, 1200), step=10)

silver = silver_all
silver = silver[silver["longitude"].between(lo0, lo1) & silver["latitude"].between(la0, la1)] \
    if region != "World" else silver
if status_f != "All":
    silver = silver[silver["on_ground"] == (status_f == "On ground")]
if picked:
    silver = silver[silver["origin_country"].isin(picked)]
if picked_air:
    silver = silver[silver["airline"].isin(picked_air)]
if search:
    q = search.strip().lower()
    silver = silver[silver["callsign"].str.lower().str.contains(q) | silver["icao24"].str.contains(q)]
silver = silver[(silver["altitude"].fillna(0).between(*alt_rng)) & (silver["velocity"].fillna(0) * 3.6).between(*spd_rng)]

# ---------- Gold ----------
airborne = silver[silver["on_ground"] == False]
anomalies = airborne[airborne["altitude"] < LOW_ALT_M][
    ["icao24", "callsign", "origin_country", "altitude", "speed_kmh"]]
gold = (silver.groupby("origin_country")
        .agg(total_aircrafts=("icao24", "count"),
             avg_altitude=("altitude", "mean"),
             max_record_altitude=("altitude", "max"),
             avg_velocity_mps=("velocity", "mean"),
             aircrafts_on_ground=("on_ground", "sum"))
        .round(2).reset_index())
gold["aircrafts_in_air"] = gold["total_aircrafts"] - gold["aircrafts_on_ground"]
gold = gold.sort_values("total_aircrafts", ascending=False)

# ---------- KPIs ----------
k = st.columns(5)
k[0].metric("Aircraft shown", f"{len(silver):,}", f"of {len(silver_all):,} worldwide")
k[1].metric("In air", f"{len(airborne):,}")
k[2].metric("Max altitude", f"{silver['altitude'].max():,.0f} m" if len(silver) else "–")
k[3].metric("Avg speed", f"{silver['speed_kmh'].mean():,.0f} km/h" if len(silver) else "–")
k[4].metric("Low-altitude alerts " + ("🔴" if len(anomalies) else "🟢"), f"{len(anomalies):,}")

tab_map, tab_an, tab_geo, tab_hist, tab_data = st.tabs(
    ["🗺️ Live map", "📈 Analytics", "🌍 Countries & airlines", "🕒 History", "📋 Data & alerts"])

with tab_map:
    m = silver.dropna(subset=["longitude", "latitude"]).copy()
    m["color"] = m.apply(lambda r: [220, 60, 60, 190] if r["on_ground"]
                         else ([255, 170, 0, 220] if (r["altitude"] or 0) < LOW_ALT_M else [40, 140, 240, 170]), axis=1)
    st.caption("🔵 in air   🔴 on ground   🟠 airborne below 500 m")
    st.pydeck_chart(pdk.Deck(
        layers=[pdk.Layer("ScatterplotLayer", m, get_position=["longitude", "latitude"],
                          get_fill_color="color", get_radius=9000 if region != "World" else 15000,
                          radius_min_pixels=3, pickable=True)],
        initial_view_state=pdk.ViewState(latitude=vlat, longitude=vlon, zoom=vzoom),
        tooltip={"text": "{callsign} ({airline})\n{origin_country}\nAlt: {altitude_ft} ft\nSpeed: {speed_kmh} km/h"},
        map_style=None))

with tab_an:
    c1, c2 = st.columns(2)
    s = silver.dropna(subset=["altitude", "velocity"])
    with c1:
        st.subheader("Altitude vs. velocity")
        st.plotly_chart(px.scatter(s, x="speed_kmh", y="altitude", color="on_ground", hover_name="callsign",
                                   opacity=0.5, labels={"speed_kmh": "Speed (km/h)", "altitude": "Altitude (m)"}),
                        use_container_width=True)
    with c2:
        st.subheader("Flight status")
        stt = silver["on_ground"].map({True: "On ground", False: "In air"}).value_counts().reset_index()
        stt.columns = ["status", "count"]
        st.plotly_chart(px.pie(stt, names="status", values="count", hole=0.5), use_container_width=True)
    c3, c4 = st.columns(2)
    with c3:
        st.subheader("Altitude distribution (airborne)")
        st.plotly_chart(px.histogram(airborne, x="altitude", nbins=40, labels={"altitude": "Altitude (m)"}),
                        use_container_width=True)
    with c4:
        st.subheader("Fastest / highest aircraft")
        st.dataframe(airborne.nlargest(10, "speed_kmh")[["callsign", "origin_country", "speed_kmh", "altitude_ft"]],
                     use_container_width=True, hide_index=True)

with tab_geo:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Top 15 countries by aircraft")
        st.plotly_chart(px.bar(gold.head(15), x="origin_country", y=["aircrafts_in_air", "aircrafts_on_ground"],
                               labels={"value": "Aircraft", "origin_country": ""}), use_container_width=True)
    with c2:
        st.subheader("Top 15 airlines (callsign prefix)")
        al = silver["airline"].value_counts().head(15).reset_index()
        al.columns = ["airline", "aircraft"]
        st.plotly_chart(px.bar(al, x="airline", y="aircraft"), use_container_width=True)
    st.subheader("Gold: fleet statistics by country")
    st.dataframe(gold, use_container_width=True, hide_index=True)

with tab_hist:
    h = load_history()
    st.caption(f"Snapshots are saved to {DB.name} on every fetch. Leave auto-refresh on to build a trend.")
    if h.empty or h["ts"].nunique() < 2:
        st.info("Need at least 2 snapshots. Click Refresh (data updates every 60 s) or enable auto-refresh.")
    else:
        scope = st.selectbox("Country", ["All countries"] + sorted(h["country"].unique()))
        hh = h if scope == "All countries" else h[h["country"] == scope]
        tr = hh.groupby("ts")[["total", "in_air", "low_alt"]].sum().reset_index()
        st.plotly_chart(px.line(tr, x="ts", y=["total", "in_air"], markers=True,
                                labels={"value": "Aircraft", "ts": "Snapshot (UTC)"}), use_container_width=True)
        st.plotly_chart(px.line(tr, x="ts", y="low_alt", markers=True,
                                labels={"low_alt": "Low-altitude alerts", "ts": "Snapshot (UTC)"}),
                        use_container_width=True)

with tab_data:
    st.subheader("Low-altitude safety alerts (< 500 m, airborne)")
    st.dataframe(anomalies, use_container_width=True, hide_index=True)
    st.subheader(f"Silver table ({len(silver):,} rows)")
    cols = ["callsign", "icao24", "airline", "origin_country", "latitude", "longitude",
            "altitude", "altitude_ft", "speed_kmh", "on_ground"]
    st.dataframe(silver[cols], use_container_width=True, hide_index=True)
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Download Silver (CSV)", silver[cols].to_csv(index=False), "silver_flights.csv", "text/csv")
    d2.download_button("⬇️ Download Gold (CSV)", gold.to_csv(index=False), "gold_fleet_statistics.csv", "text/csv")

if auto:
    time.sleep(60)
    fetch_bronze.clear()
    st.rerun()
