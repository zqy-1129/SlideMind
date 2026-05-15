import re
from datetime import datetime
from typing import Any


INSAR_TIME_FIELD_PATTERN = re.compile(r"^D_(\d{8})$")


def extract_insar_observations(raw_fields: dict[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for key, value in raw_fields.items():
        match = INSAR_TIME_FIELD_PATTERN.match(str(key))
        if not match or not _is_number(value):
            continue
        date_text = match.group(1)
        date = datetime.strptime(date_text, "%Y%m%d").date()
        points.append({"date": date.isoformat(), "date_obj": date, "value": float(value)})
    points.sort(key=lambda item: item["date"])

    previous = None
    observations: list[dict[str, Any]] = []
    for point in points:
        delta = 0.0 if previous is None else float(point["value"]) - float(previous["value"])
        days = 0 if previous is None else max((point["date_obj"] - previous["date_obj"]).days, 1)
        rate = 0.0 if previous is None else delta / days
        observations.append(
            {
                "date": point["date"],
                "value": round(float(point["value"]), 4),
                "delta": round(delta, 4),
                "rate": round(rate, 6),
            }
        )
        previous = point
    return observations


def summarize_insar_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {
            "start_date": None,
            "end_date": None,
            "observation_count": 0,
            "latest_value": None,
            "max_settlement": None,
            "max_uplift": None,
            "cumulative_change": None,
            "average_rate": None,
            "trend": "无时序",
        }

    values = [float(item["value"]) for item in observations]
    start_value = values[0]
    latest_value = values[-1]
    start_date = datetime.fromisoformat(str(observations[0]["date"])).date()
    end_date = datetime.fromisoformat(str(observations[-1]["date"])).date()
    days = max((end_date - start_date).days, 1)
    cumulative_change = latest_value - start_value
    average_rate = cumulative_change / days
    return {
        "start_date": observations[0]["date"],
        "end_date": observations[-1]["date"],
        "start_value": round(start_value, 4),
        "latest_value": round(latest_value, 4),
        "max_settlement": round(min(values), 4),
        "max_uplift": round(max(values), 4),
        "cumulative_change": round(cumulative_change, 4),
        "average_rate": round(average_rate, 6),
        "observation_count": len(observations),
        "trend": classify_insar_trend(cumulative_change, average_rate),
    }


def build_insar_time_series_document(record: dict[str, Any]) -> dict[str, Any] | None:
    raw = record.get("raw_fields") or {}
    normalized = record.get("normalized_fields") or {}
    observations = extract_insar_observations(raw)
    if not observations:
        return None
    summary = summarize_insar_observations(observations)
    return {
        "dataset_id": record.get("dataset_id"),
        "source_file_id": record.get("source_file_id"),
        "source_record_id": str(record.get("_id")),
        "point_id": normalized.get("point_id") or raw.get("point_id") or raw.get("id") or str(record.get("_id")),
        "longitude": normalized.get("longitude") or raw.get("lon") or raw.get("longitude") or raw.get("lng"),
        "latitude": normalized.get("latitude") or raw.get("lat") or raw.get("latitude"),
        **summary,
        "observations": observations,
        "created_at": datetime.utcnow(),
    }


def summarize_insar_raw_fields(raw_fields: dict[str, Any]) -> dict[str, Any]:
    return summarize_insar_observations(extract_insar_observations(raw_fields))


def strip_insar_time_fields(raw_fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw_fields.items() if not INSAR_TIME_FIELD_PATTERN.match(str(key))}


def classify_insar_trend(cumulative_change: float, average_rate: float) -> str:
    if cumulative_change <= -10 or average_rate <= -0.01:
        return "持续沉降"
    if cumulative_change >= 10 or average_rate >= 0.01:
        return "持续抬升"
    if abs(cumulative_change) >= 3:
        return "轻微变化"
    return "基本稳定"


def _is_number(value: Any) -> bool:
    try:
        if value is None or value == "":
            return False
        float(value)
        return True
    except (TypeError, ValueError):
        return False
