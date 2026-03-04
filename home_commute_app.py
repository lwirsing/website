from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="RI Home Commute Explorer", layout="wide")

OFFICE_ADDRESS = "200 Callahan Rd, North Kingstown, RI 02852"
RI_TZ = ZoneInfo("America/New_York")

POPULAR_RI_BEACHES = [
    "Misquamicut State Beach, Westerly, RI",
    "Scarborough State Beach, Narragansett, RI",
    "Narragansett Town Beach, Narragansett, RI",
    "East Matunuck State Beach, South Kingstown, RI",
    "Roger W. Wheeler State Beach, Narragansett, RI",
    "Sachuest Beach (Second Beach), Middletown, RI",
]
SAVED_DB_PATH = Path("homesearch_data.db")


@dataclass
class Place:
    name: str
    address: str
    lat: float
    lng: float


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SAVED_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_saved_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_properties (
                home TEXT PRIMARY KEY,
                name TEXT,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                saved_at TEXT NOT NULL,
                analysis_day TEXT,
                zillow_url TEXT,
                commute_json TEXT
            )
            """
        )


def load_saved_properties() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM saved_properties ORDER BY saved_at DESC").fetchall()

    saved: dict[str, dict] = {}
    for row in rows:
        commute = {}
        if row["commute_json"]:
            try:
                commute = json.loads(row["commute_json"])
            except json.JSONDecodeError:
                commute = {}
        saved[row["home"]] = {
            "home": row["home"],
            "name": row["name"] or row["home"],
            "lat": float(row["lat"]),
            "lng": float(row["lng"]),
            "saved_at": row["saved_at"],
            "analysis_day": row["analysis_day"],
            "zillow_url": row["zillow_url"] or zillow_link(row["home"]),
            "commute": commute,
        }
    return saved


def upsert_saved_property(record: dict) -> None:
    commute_json = json.dumps(record.get("commute", {}))
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO saved_properties(home, name, lat, lng, saved_at, analysis_day, zillow_url, commute_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(home) DO UPDATE SET
                name=excluded.name,
                lat=excluded.lat,
                lng=excluded.lng,
                saved_at=excluded.saved_at,
                analysis_day=excluded.analysis_day,
                zillow_url=excluded.zillow_url,
                commute_json=excluded.commute_json
            """,
            (
                record["home"],
                record.get("name", record["home"]),
                float(record["lat"]),
                float(record["lng"]),
                record["saved_at"],
                record.get("analysis_day"),
                record.get("zillow_url", zillow_link(record["home"])),
                commute_json,
            ),
        )


def delete_saved_property(home: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM saved_properties WHERE home = ?", (home,))


def next_weekday(start: date) -> date:
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def to_unix_ts(local_date: date, local_time: time) -> int:
    dt = datetime.combine(local_date, local_time, tzinfo=RI_TZ)
    return int(dt.timestamp())


@st.cache_data(show_spinner=False)
def geocode_address(address: str, api_key: str) -> Place:
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "key": api_key},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("status") != "OK" or not payload.get("results"):
        raise ValueError(f"Geocoding failed for '{address}': {payload.get('status')}")

    result = payload["results"][0]
    loc = result["geometry"]["location"]
    return Place(
        name=address,
        address=result.get("formatted_address", address),
        lat=loc["lat"],
        lng=loc["lng"],
    )


@st.cache_data(show_spinner=False)
def distance_matrix(
    origin: str,
    destination: str,
    api_key: str,
    departure_time: int | None = None,
) -> dict:
    params = {
        "origins": origin,
        "destinations": destination,
        "mode": "driving",
        "units": "imperial",
        "key": api_key,
    }
    if departure_time is not None:
        params["departure_time"] = departure_time
        params["traffic_model"] = "best_guess"

    resp = requests.get(
        "https://maps.googleapis.com/maps/api/distancematrix/json",
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("status") != "OK":
        raise ValueError(f"Distance Matrix request failed: {payload.get('status')}")

    element = payload["rows"][0]["elements"][0]
    if element.get("status") != "OK":
        raise ValueError(f"Route lookup failed ({origin} -> {destination}): {element.get('status')}")

    return element


@st.cache_data(show_spinner=False)
def distance_matrix_multi_destinations(
    origin: str,
    destinations: list[str],
    api_key: str,
) -> list[dict]:
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/distancematrix/json",
        params={
            "origins": origin,
            "destinations": "|".join(destinations),
            "mode": "driving",
            "units": "imperial",
            "key": api_key,
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("status") != "OK":
        raise ValueError(f"Distance Matrix request failed: {payload.get('status')}")

    return payload["rows"][0]["elements"]


def duration_minutes(element: dict) -> float:
    with_traffic = element.get("duration_in_traffic", element.get("duration", {}))
    return with_traffic.get("value", 0) / 60.0


def duration_text(element: dict) -> str:
    with_traffic = element.get("duration_in_traffic")
    if with_traffic and with_traffic.get("text"):
        return with_traffic["text"]
    return element.get("duration", {}).get("text", "n/a")


def miles(distance_value_meters: int) -> float:
    return distance_value_meters * 0.000621371


def zillow_link(address: str) -> str:
    return f"https://www.zillow.com/homes/{quote_plus(address)}_rb/"


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += delta_lat

        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += delta_lng

        points.append((lat / 1e5, lng / 1e5))

    return points


@st.cache_data(show_spinner=False)
def directions_route_points(origin: str, destination: str, api_key: str) -> list[tuple[float, float]]:
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/directions/json",
        params={
            "origin": origin,
            "destination": destination,
            "mode": "driving",
            "key": api_key,
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "OK" or not payload.get("routes"):
        raise ValueError(f"Directions lookup failed ({origin} -> {destination}): {payload.get('status')}")

    polyline = payload["routes"][0]["overview_polyline"]["points"]
    return decode_polyline(polyline)


def straight_line_points(origin: Place, destination: Place) -> list[tuple[float, float]]:
    return [(origin.lat, origin.lng), (destination.lat, destination.lng)]


def build_map_points(
    selected_home: Place,
    office: Place,
    beaches: list[Place],
) -> pd.DataFrame:
    rows = [
        {"name": "Selected Home", "type": "Home", "lat": selected_home.lat, "lon": selected_home.lng},
        {"name": "Office", "type": "Office", "lat": office.lat, "lon": office.lng},
    ]
    for beach in beaches:
        rows.append({"name": beach.name, "type": "Beach", "lat": beach.lat, "lon": beach.lng})
    return pd.DataFrame(rows)


def build_map_figure(points: pd.DataFrame, routes: list[dict]):
    center = {"lat": points["lat"].mean(), "lon": points["lon"].mean()}
    fig = px.scatter_mapbox(
        points,
        lat="lat",
        lon="lon",
        color="type",
        text="name",
        hover_name="name",
        zoom=9,
        center=center,
        color_discrete_map={
            "Home": "#1f77b4",
            "Office": "#d62728",
            "Beach": "#f4a300",
        },
    )
    fig.update_layout(
        mapbox_style="open-street-map",
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        legend_title_text="Location Type",
    )
    fig.update_traces(marker={"size": 14}, textposition="top right")
    for route in routes:
        if not route["coords"]:
            continue
        lat_vals = [coord[0] for coord in route["coords"]]
        lon_vals = [coord[1] for coord in route["coords"]]
        fig.add_trace(
            go.Scattermapbox(
                lat=lat_vals,
                lon=lon_vals,
                mode="lines",
                line={"width": 3, "color": route["color"]},
                name=route["name"],
                hovertemplate=f"{route['name']}<extra></extra>",
                showlegend=True,
            )
        )
    return fig


def main() -> None:
    st.title("Rhode Island Home Commute + Beach Distance Explorer")
    st.caption(
        "Compare commute times to your office with traffic-aware estimates and view distances to popular RI beaches."
    )

    if "saved_properties" not in st.session_state:
        st.session_state["saved_properties"] = {}
    if "analysis_data" not in st.session_state:
        st.session_state["analysis_data"] = None
    init_saved_db()
    st.session_state["saved_properties"] = load_saved_properties()

    default_api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not default_api_key:
        try:
            default_api_key = st.secrets["GOOGLE_MAPS_API_KEY"]
        except Exception:
            default_api_key = ""

    with st.sidebar:
        st.header("Settings")
        api_key = st.text_input(
            "Google Maps API key",
            type="password",
            value=default_api_key,
            help="Enable Geocoding API, Distance Matrix API, and Directions API in your Google Cloud project.",
        )
        morning_time = st.time_input("Morning rush departure", value=time(8, 30))
        evening_time = st.time_input("Evening rush departure", value=time(17, 30))
        offpeak_time = st.time_input("Off-peak departure", value=time(11, 0))

    addresses_raw = st.text_area(
        "Candidate home addresses (one per line)",
        placeholder="123 Main St, East Greenwich, RI\n45 Ocean Ave, Narragansett, RI",
        height=140,
        key="candidate_addresses",
    )

    run = st.button("Analyze Commutes + Beaches", type="primary")

    if run:
        if not api_key:
            st.error("Please add a Google Maps API key in the sidebar.")
            return

        home_addresses = [line.strip() for line in addresses_raw.splitlines() if line.strip()]
        if not home_addresses:
            st.error("Please enter at least one home address.")
            return

        try:
            analysis_day = next_weekday(date.today() + timedelta(days=1))
            morning_ts = to_unix_ts(analysis_day, morning_time)
            evening_ts = to_unix_ts(analysis_day, evening_time)
            offpeak_ts = to_unix_ts(analysis_day, offpeak_time)

            office = geocode_address(OFFICE_ADDRESS, api_key)
            beaches = [geocode_address(b, api_key) for b in POPULAR_RI_BEACHES]

            commute_rows: list[dict] = []
            beach_rows: list[dict] = []
            geocoded_homes: list[Place] = []

            progress = st.progress(0.0)
            total = len(home_addresses)

            for idx, home in enumerate(home_addresses, start=1):
                place = geocode_address(home, api_key)
                geocoded_homes.append(place)

                morning = distance_matrix(place.address, office.address, api_key, departure_time=morning_ts)
                evening = distance_matrix(office.address, place.address, api_key, departure_time=evening_ts)
                offpeak_to = distance_matrix(place.address, office.address, api_key, departure_time=offpeak_ts)
                offpeak_from = distance_matrix(office.address, place.address, api_key, departure_time=offpeak_ts)

                beach_elements = distance_matrix_multi_destinations(
                    place.address,
                    [b.address for b in beaches],
                    api_key,
                )

                rush_roundtrip_min = duration_minutes(morning) + duration_minutes(evening)
                offpeak_roundtrip_min = duration_minutes(offpeak_to) + duration_minutes(offpeak_from)

                commute_rows.append(
                    {
                        "Home": place.address,
                        "Morning Rush (to office)": duration_text(morning),
                        "Evening Rush (from office)": duration_text(evening),
                        "Off-Peak To Office": duration_text(offpeak_to),
                        "Off-Peak From Office": duration_text(offpeak_from),
                        "One-Way Distance": morning["distance"]["text"],
                        "Rush Roundtrip (min)": round(rush_roundtrip_min, 1),
                        "Off-Peak Roundtrip (min)": round(offpeak_roundtrip_min, 1),
                        "Traffic Penalty (min)": round(rush_roundtrip_min - offpeak_roundtrip_min, 1),
                    }
                )

                for beach, element in zip(beaches, beach_elements):
                    if element.get("status") != "OK":
                        continue
                    beach_rows.append(
                        {
                            "Home": place.address,
                            "Beach": beach.name,
                            "Distance": element["distance"]["text"],
                            "Est. Drive Time": element["duration"]["text"],
                            "Distance (miles)": round(miles(element["distance"]["value"]), 2),
                        }
                    )

                progress.progress(idx / total)

            st.session_state["analysis_data"] = {
                "analysis_day": analysis_day.isoformat(),
                "office": {
                    "name": office.name,
                    "address": office.address,
                    "lat": office.lat,
                    "lng": office.lng,
                },
                "beaches": [
                    {
                        "name": beach.name,
                        "address": beach.address,
                        "lat": beach.lat,
                        "lng": beach.lng,
                    }
                    for beach in beaches
                ],
                "geocoded_homes": [
                    {
                        "name": home.name,
                        "address": home.address,
                        "lat": home.lat,
                        "lng": home.lng,
                    }
                    for home in geocoded_homes
                ],
                "commute_rows": commute_rows,
                "beach_rows": beach_rows,
            }
        except requests.RequestException as exc:
            st.error(f"Network/API error: {exc}")
            return
        except ValueError as exc:
            st.error(str(exc))
            return

    if st.session_state["analysis_data"]:
        analysis_data = st.session_state["analysis_data"]
        analysis_day = date.fromisoformat(analysis_data["analysis_day"])
        office = Place(**analysis_data["office"])
        beaches = [Place(**beach) for beach in analysis_data["beaches"]]
        geocoded_homes = [Place(**home) for home in analysis_data["geocoded_homes"]]
        geocoded_home_index = {home.address: home for home in geocoded_homes}
        commute_df = pd.DataFrame(analysis_data["commute_rows"]).sort_values("Rush Roundtrip (min)")
        beach_df = pd.DataFrame(analysis_data["beach_rows"]).sort_values(["Home", "Distance (miles)"])

        st.success(f"Analyzed {len(commute_df)} home(s) for {analysis_day.isoformat()} traffic assumptions.")

        st.subheader("Commute Comparison")
        st.dataframe(commute_df, use_container_width=True)

        st.subheader("Beach Distance Comparison")
        st.dataframe(beach_df[["Home", "Beach", "Distance", "Est. Drive Time"]], use_container_width=True)

        st.subheader("Map View")
        all_homes = commute_df["Home"].tolist()
        saved_homes_all = list(st.session_state["saved_properties"].keys())
        show_saved_only = st.toggle(
            "Show only saved homes in dropdown",
            value=False,
            disabled=not saved_homes_all,
        )
        if show_saved_only and saved_homes_all:
            home_options = saved_homes_all
        else:
            home_options = all_homes + [home for home in saved_homes_all if home not in all_homes]

        selected_home_text = st.selectbox("Select a home to visualize", home_options)
        selected_home = geocoded_home_index.get(selected_home_text)
        if selected_home is None and selected_home_text in st.session_state["saved_properties"]:
            saved = st.session_state["saved_properties"][selected_home_text]
            selected_home = Place(
                name=saved.get("name", selected_home_text),
                address=saved["home"],
                lat=float(saved["lat"]),
                lng=float(saved["lng"]),
            )
        if selected_home is None:
            st.error("Unable to load selected property details. Please analyze that address again.")
            return

        map_points = build_map_points(selected_home, office, beaches)

        using_fallback = False
        routes: list[dict] = []
        try:
            routes.append(
                {
                    "name": "Home -> Office",
                    "coords": directions_route_points(selected_home.address, office.address, api_key),
                    "color": "#8b0000",
                }
            )
            for beach in beaches:
                routes.append(
                    {
                        "name": f"Home -> {beach.name}",
                        "coords": directions_route_points(selected_home.address, beach.address, api_key),
                        "color": "#666666",
                    }
                )
        except ValueError:
            using_fallback = True
            routes.append(
                {
                    "name": "Home -> Office",
                    "coords": straight_line_points(selected_home, office),
                    "color": "#8b0000",
                }
            )
            for beach in beaches:
                routes.append(
                    {
                        "name": f"Home -> {beach.name}",
                        "coords": straight_line_points(selected_home, beach),
                        "color": "#666666",
                    }
                )

        st.plotly_chart(build_map_figure(map_points, routes), use_container_width=True)
        if using_fallback:
            st.warning(
                "Could not load turn-by-turn routes from Google Directions API. "
                "Showing straight-line paths instead."
            )

        commute_row = commute_df.loc[commute_df["Home"] == selected_home_text]
        selected_commute_row = (
            commute_row.iloc[0].to_dict()
            if not commute_row.empty
            else st.session_state["saved_properties"].get(selected_home_text, {}).get("commute", {})
        )
        if st.button("Save Selected Property"):
            record = {
                "home": selected_home_text,
                "name": selected_home.name,
                "lat": selected_home.lat,
                "lng": selected_home.lng,
                "saved_at": datetime.now(RI_TZ).isoformat(timespec="seconds"),
                "analysis_day": analysis_day.isoformat(),
                "zillow_url": zillow_link(selected_home_text),
                "commute": selected_commute_row,
            }
            upsert_saved_property(record)
            st.session_state["saved_properties"] = load_saved_properties()
            st.success("Saved property. Zillow link and comparison tools are available below.")
    else:
        st.info("Enter at least one home address and click Analyze to refresh commute and map data.")

    if st.session_state["saved_properties"]:
        st.subheader("Saved Properties")
        saved_rows = []
        for home, rec in st.session_state["saved_properties"].items():
            commute = rec.get("commute", {})
            saved_rows.append(
                {
                    "Home": home,
                    "Analysis Day": rec.get("analysis_day"),
                    "Morning Rush (to office)": commute.get("Morning Rush (to office)"),
                    "Evening Rush (from office)": commute.get("Evening Rush (from office)"),
                    "Rush Roundtrip (min)": commute.get("Rush Roundtrip (min)"),
                    "Off-Peak Roundtrip (min)": commute.get("Off-Peak Roundtrip (min)"),
                    "Traffic Penalty (min)": commute.get("Traffic Penalty (min)"),
                    "Zillow": rec.get("zillow_url"),
                }
            )

        saved_df = pd.DataFrame(saved_rows).sort_values("Rush Roundtrip (min)", na_position="last")
        st.dataframe(
            saved_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Zillow": st.column_config.LinkColumn("Zillow"),
            },
        )

        to_delete = st.multiselect(
            "Delete saved properties",
            options=saved_df["Home"].tolist(),
            default=[],
            key="delete_saved_properties",
        )
        if st.button("Delete Selected", disabled=not to_delete):
            for home in to_delete:
                delete_saved_property(home)
            st.session_state["saved_properties"] = load_saved_properties()
            st.success("Deleted selected saved properties.")
            st.rerun()

        st.subheader("Compare Saved Property Commutes")
        compare_options = saved_df["Home"].tolist()
        default_compare = compare_options[:2] if len(compare_options) >= 2 else compare_options
        selected_compare = st.multiselect(
            "Select saved properties to compare",
            options=compare_options,
            default=default_compare,
        )

        if len(selected_compare) >= 2:
            compare_df = saved_df[saved_df["Home"].isin(selected_compare)][
                [
                    "Home",
                    "Rush Roundtrip (min)",
                    "Off-Peak Roundtrip (min)",
                    "Traffic Penalty (min)",
                ]
            ]
            st.dataframe(compare_df, use_container_width=True, hide_index=True)

            compare_long = compare_df.melt(
                id_vars=["Home"],
                var_name="Metric",
                value_name="Minutes",
            )
            compare_fig = px.bar(
                compare_long,
                x="Home",
                y="Minutes",
                color="Metric",
                barmode="group",
            )
            compare_fig.update_layout(
                margin={"l": 0, "r": 0, "t": 20, "b": 0},
                yaxis_title="Minutes",
                xaxis_title="Property",
            )
            st.plotly_chart(compare_fig, use_container_width=True)
        elif len(compare_options) >= 2:
            st.info("Select at least two saved properties to compare commute times.")

    st.caption(
        "Rush-hour values use Google traffic-aware estimates (best_guess) for the selected times; "
        "beach distances use standard driving estimates."
    )


if __name__ == "__main__":
    main()
