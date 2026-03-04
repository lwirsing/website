from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import os
from zoneinfo import ZoneInfo

import pandas as pd
import pydeck as pdk
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


@dataclass
class Place:
    name: str
    address: str
    lat: float
    lng: float


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


def build_map(
    selected_home: Place,
    office: Place,
    beaches: list[Place],
) -> pdk.Deck:
    points = [
        {
            "name": "Office",
            "kind": "office",
            "lat": office.lat,
            "lng": office.lng,
            "color": [220, 20, 60],
        },
        {
            "name": "Selected Home",
            "kind": "home",
            "lat": selected_home.lat,
            "lng": selected_home.lng,
            "color": [0, 123, 255],
        },
    ]

    for beach in beaches:
        points.append(
            {
                "name": beach.name,
                "kind": "beach",
                "lat": beach.lat,
                "lng": beach.lng,
                "color": [245, 160, 0],
            }
        )

    lines = [
        {
            "source_lat": selected_home.lat,
            "source_lng": selected_home.lng,
            "target_lat": office.lat,
            "target_lng": office.lng,
            "label": "Home ↔ Office",
            "color": [180, 40, 40],
        }
    ]

    for beach in beaches:
        lines.append(
            {
                "source_lat": selected_home.lat,
                "source_lng": selected_home.lng,
                "target_lat": beach.lat,
                "target_lng": beach.lng,
                "label": f"Home ↔ {beach.name}",
                "color": [120, 120, 120],
            }
        )

    point_layer = pdk.Layer(
        "ScatterplotLayer",
        data=points,
        get_position="[lng, lat]",
        get_fill_color="color",
        get_radius=450,
        pickable=True,
    )

    text_layer = pdk.Layer(
        "TextLayer",
        data=points,
        get_position="[lng, lat]",
        get_text="name",
        get_color=[20, 20, 20],
        get_size=14,
        get_alignment_baseline="bottom",
        get_pixel_offset=[0, -10],
    )

    line_layer = pdk.Layer(
        "LineLayer",
        data=lines,
        get_source_position="[source_lng, source_lat]",
        get_target_position="[target_lng, target_lat]",
        get_color="color",
        get_width=3,
        pickable=True,
    )

    center_lat = (selected_home.lat + office.lat) / 2
    center_lng = (selected_home.lng + office.lng) / 2

    return pdk.Deck(
        map_provider="carto",
        map_style="light",
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lng, zoom=9),
        layers=[line_layer, point_layer, text_layer],
        tooltip={"text": "{name}"},
    )


def main() -> None:
    st.title("Rhode Island Home Commute + Beach Distance Explorer")
    st.caption(
        "Compare commute times to your office with traffic-aware estimates and view distances to popular RI beaches."
    )

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
            help="Enable Geocoding API and Distance Matrix API in your Google Cloud project.",
        )
        morning_time = st.time_input("Morning rush departure", value=time(8, 30))
        evening_time = st.time_input("Evening rush departure", value=time(17, 30))
        offpeak_time = st.time_input("Off-peak departure", value=time(11, 0))

    st.subheader("Office")
    st.code(OFFICE_ADDRESS)

    addresses_raw = st.text_area(
        "Candidate home addresses (one per line)",
        placeholder="123 Main St, East Greenwich, RI\n45 Ocean Ave, Narragansett, RI",
        height=140,
    )

    run = st.button("Analyze Commutes + Beaches", type="primary")

    if not run:
        st.info("Enter at least one home address and click Analyze.")
        return

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

        commute_df = pd.DataFrame(commute_rows).sort_values("Rush Roundtrip (min)")
        beach_df = pd.DataFrame(beach_rows).sort_values(["Home", "Distance (miles)"])

        st.success(f"Analyzed {len(commute_df)} home(s) for {analysis_day.isoformat()} traffic assumptions.")

        st.subheader("Commute Comparison")
        st.dataframe(commute_df, use_container_width=True)

        st.subheader("Beach Distance Comparison")
        st.dataframe(beach_df[["Home", "Beach", "Distance", "Est. Drive Time"]], use_container_width=True)

        st.subheader("Map View")
        selected_home_text = st.selectbox("Select a home to visualize", commute_df["Home"].tolist())
        selected_home = next(h for h in geocoded_homes if h.address == selected_home_text)
        st.pydeck_chart(build_map(selected_home, office, beaches), use_container_width=True)

        st.caption(
            "Rush-hour values use Google traffic-aware estimates (best_guess) for the selected times; "
            "beach distances use standard driving estimates."
        )

    except requests.RequestException as exc:
        st.error(f"Network/API error: {exc}")
    except ValueError as exc:
        st.error(str(exc))


if __name__ == "__main__":
    main()
