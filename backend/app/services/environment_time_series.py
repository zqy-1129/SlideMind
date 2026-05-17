from datetime import datetime
from typing import Any


TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)

VALUE_ALIASES = {
    "rainfall": ("rainfall", "rain", "rain_sum", "rain_sum (mm)", "降雨", "降雨量", "雨量"),
    "water_level": ("water_level", "level", "height", "height(m)", "height (m)", "库水位", "水位"),
}


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "").replace("T", " "))
    except ValueError:
        return None


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def build_environment_time_series_document(
    dataset_id: str,
    dataset_name: str,
    source_file_id: str,
    data_type: str,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if data_type not in {"rainfall", "water_level"}:
        return None
    observations = _records_to_observations(data_type, records)
    if not observations:
        return None
    summary = summarize_environment_observations(data_type, observations)
    name_suffix = "降雨时序" if data_type == "rainfall" else "库水位时序"
    return {
        "dataset_id": dataset_id,
        "source_file_id": source_file_id,
        "data_type": data_type,
        "name": f"{dataset_name}_{name_suffix}",
        **summary,
        "observations": observations,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }


def summarize_environment_observations(data_type: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["value"]) for item in observations]
    latest_value = values[-1]
    cumulative_value = sum(values) if data_type == "rainfall" else latest_value - values[0]
    average_value = sum(values) / len(values)
    return {
        "start_date": observations[0]["datetime"],
        "end_date": observations[-1]["datetime"],
        "observation_count": len(observations),
        "latest_value": round(latest_value, 4),
        "max_value": round(max(values), 4),
        "min_value": round(min(values), 4),
        "average_value": round(average_value, 4),
        "cumulative_value": round(cumulative_value, 4),
        "trend": _classify_trend(data_type, values, cumulative_value),
    }


def _records_to_observations(data_type: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[tuple[datetime, float]] = []
    for record in records:
        normalized = record.get("normalized_fields") or {}
        raw = record.get("raw_fields") or {}
        timestamp = normalized.get("timestamp") or _find_value(raw, ("timestamp", "time", "date", "datetime"))
        parsed_time = parse_time(timestamp)
        value = normalized.get(data_type)
        if value is None or value == "":
            value = _find_value(raw, VALUE_ALIASES[data_type])
        if parsed_time is None or not _is_number(value):
            continue
        rows.append((parsed_time, float(value)))
    rows.sort(key=lambda item: item[0])

    observations: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    previous_value: float | None = None
    cumulative = 0.0
    first_value = rows[0][1] if rows else 0.0
    for current_time, value in rows:
        delta = 0.0 if previous_value is None else value - previous_value
        days = 0 if previous_time is None else max((current_time - previous_time).total_seconds() / 86400, 1 / 24)
        rate = 0.0 if previous_value is None else delta / days
        cumulative = cumulative + value if data_type == "rainfall" else value - first_value
        observations.append(
            {
                "date": current_time.date().isoformat(),
                "datetime": format_datetime(current_time),
                "value": round(value, 4),
                "delta": round(delta, 4),
                "rate": round(rate, 6),
                "cumulative": round(cumulative, 4),
            }
        )
        previous_time = current_time
        previous_value = value
    return observations


def _find_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    lower = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        key = alias.lower()
        if key in lower:
            return lower[key]
    return None


def _is_number(value: Any) -> bool:
    try:
        if value is None or value == "":
            return False
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _classify_trend(data_type: str, values: list[float], cumulative_value: float) -> str:
    if data_type == "rainfall":
        return "持续降雨" if cumulative_value > 0 else "无明显降雨"
    change = values[-1] - values[0]
    if change > 1:
        return "水位上升"
    if change < -1:
        return "水位下降"
    return "水位平稳"
