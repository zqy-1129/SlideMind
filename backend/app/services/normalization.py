from datetime import datetime
from typing import Any


FIELD_ALIASES = {
    "timestamp": ["timestamp", "time", "date", "datetime", "观测时间", "时间", "日期"],
    "point_id": ["point_id", "point", "monitor_point", "监测点", "点号", "监测点编号", "insar点号"],
    "landslide_name": ["landslide", "landslide_name", "滑坡", "滑坡体", "滑坡名称"],
    "longitude": ["longitude", "lon", "lng", "经度"],
    "latitude": ["latitude", "lat", "纬度"],
    "elevation": ["elevation", "height", "高程"],
    "displacement": ["displacement", "deformation", "形变", "累计形变", "位移", "累计位移"],
    "velocity": ["velocity", "rate", "形变速率", "速率"],
    "water_level": ["water_level", "level", "库水位", "水位"],
    "station_name": ["station", "station_name", "站点", "水位站"],
}


def normalize_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    lower_map = {str(key).strip().lower(): value for key, value in row.items()}
    original_map = {str(key).strip(): value for key, value in row.items()}

    for target, aliases in FIELD_ALIASES.items():
        value = None
        for alias in aliases:
            if alias.lower() in lower_map:
                value = lower_map[alias.lower()]
                break
            if alias in original_map:
                value = original_map[alias]
                break
        if value is not None and value == value:
            normalized[target] = _coerce_value(target, value)

    return normalized


def _coerce_value(target: str, value: Any) -> Any:
    if target == "timestamp":
        return _parse_datetime(value)
    if target in {"longitude", "latitude", "elevation", "displacement", "velocity", "water_level"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return str(value).strip()


def _parse_datetime(value: Any) -> datetime | str:
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return str(value).strip()

