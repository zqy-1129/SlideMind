from typing import Any

from bson import ObjectId
from fastapi import APIRouter, HTTPException

from app.db.mongo import get_db
from app.services.insar_time_series import summarize_insar_raw_fields

router = APIRouter()

GIS_LAYER_CONFIG = {
    "area": ("areas", "行政区"),
    "water": ("waters", "水域"),
    "traffic": ("traffics", "交通"),
    "build": ("buildings", "建筑"),
}
NAME_FIELDS = ("name", "NAME", "Name", "名称", "name_1", "name_2", "id", "ID")


@router.get("/map/layers")
async def get_map_layers(
    dataset_id: str,
    simplify: bool = True,
    feature_limit: int = 2000,
) -> dict[str, Any]:
    if not ObjectId.is_valid(dataset_id):
        raise HTTPException(status_code=400, detail="Invalid dataset_id")

    safe_limit = min(max(feature_limit, 100), 5000)
    layers: dict[str, Any] = {
        "areas": _feature_collection(),
        "waters": _feature_collection(),
        "traffics": _feature_collection(),
        "buildings": _feature_collection(),
        "insar_points": _feature_collection(),
        "bounds": None,
        "counts": {},
    }
    all_bounds: list[list[float]] = []

    for category, (layer_key, category_name) in GIS_LAYER_CONFIG.items():
        query = {"dataset_id": dataset_id, "gis_category": category}
        total = await get_db().gis_features.count_documents(query)
        limit = total if category == "area" else safe_limit
        cursor = get_db().gis_features.find(query).sort("feature_index", 1).limit(limit)
        features = []
        async for document in cursor:
            feature = _gis_feature(document, category_name, simplify)
            if feature is None:
                continue
            features.append(feature)
            bounds = document.get("bbox") or _geometry_bbox(feature.get("geometry"))
            if bounds:
                all_bounds.append(bounds)
        layers[layer_key] = _feature_collection(features)
        layers["counts"][layer_key] = {"loaded": len(features), "total": total}

    insar_features = []
    insar_cursor = get_db().tabular_records.find({"dataset_id": dataset_id, "data_type": "insar"}).sort("row_number", 1)
    async for record in insar_cursor:
        feature = _insar_feature(record)
        if feature is None:
            continue
        insar_features.append(feature)
        lon, lat = feature["geometry"]["coordinates"]
        all_bounds.append([lon, lat, lon, lat])
    layers["insar_points"] = _feature_collection(insar_features)
    layers["counts"]["insar_points"] = {"loaded": len(insar_features), "total": len(insar_features)}
    layers["bounds"] = _merge_bounds(all_bounds)
    return layers


def _feature_collection(features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features or []}


def _gis_feature(document: dict[str, Any], category_name: str, simplify: bool) -> dict[str, Any] | None:
    geometry = document.get("geometry")
    if not isinstance(geometry, dict):
        return None
    display_geometry = _simplify_geometry(geometry) if simplify else geometry
    properties = document.get("properties") or {}
    name = _feature_name(properties, document.get("layer_name"), document.get("feature_index"), category_name)
    return {
        "type": "Feature",
        "geometry": display_geometry,
        "properties": {
            "id": str(document["_id"]),
            "name": name,
            "layer_type": document.get("gis_category"),
            "layer_type_name": category_name,
            "geometry_type": document.get("geometry_type") or geometry.get("type"),
            "source_file_id": document.get("source_file_id"),
            "layer_name": document.get("layer_name"),
            "centroid": document.get("centroid"),
            "bbox": document.get("bbox"),
            "attributes": _important_properties(properties),
        },
    }


def _insar_feature(record: dict[str, Any]) -> dict[str, Any] | None:
    normalized = record.get("normalized_fields") or {}
    raw = record.get("raw_fields") or {}
    lon = _first_number(normalized.get("longitude"), raw.get("lon"), raw.get("longitude"), raw.get("lng"), raw.get("经度"))
    lat = _first_number(normalized.get("latitude"), raw.get("lat"), raw.get("latitude"), raw.get("纬度"))
    if lon is None or lat is None:
        return None
    point_id = normalized.get("point_id") or raw.get("point_id") or raw.get("id") or record.get("row_number")
    summary = summarize_insar_raw_fields(raw)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": str(record["_id"]),
            "source_record_id": str(record["_id"]),
            "source_file_id": record.get("source_file_id"),
            "name": f"InSAR监测点_{point_id}",
            "layer_type": "insar",
            "layer_type_name": "InSAR监测点",
            "point_id": str(point_id),
            "longitude": lon,
            "latitude": lat,
            "velocity": _first_number(normalized.get("velocity"), raw.get("velocity"), raw.get("rate"), raw.get("速率")),
            "displacement": _first_number(normalized.get("displacement"), raw.get("displacement"), raw.get("deformation")),
            "observation_count": summary.get("observation_count"),
            "latest_value": summary.get("latest_value"),
            "trend": summary.get("trend"),
        },
    }


def _feature_name(properties: dict[str, Any], layer_name: str | None, index: int | None, category_name: str) -> str:
    for field in NAME_FIELDS:
        value = properties.get(field)
        if value not in (None, ""):
            text = str(value).strip()
            if text and not text.isdigit():
                return text
    admin = properties.get("name_2") or properties.get("admin_belong")
    if admin:
        return f"{admin}_{category_name}"
    return f"{layer_name or category_name}_{index or 1}"


def _important_properties(properties: dict[str, Any]) -> dict[str, Any]:
    keys = ("name", "NAME", "Name", "name_1", "name_2", "fclass", "code", "id", "type", "class")
    return {key: properties[key] for key in keys if key in properties and properties[key] not in (None, "")}


def _simplify_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    try:
        from shapely.geometry import mapping, shape

        geom = shape(geometry)
        simplified = geom.simplify(0.00008, preserve_topology=True)
        return mapping(simplified)
    except Exception:
        return geometry


def _first_number(*values: Any) -> float | None:
    for value in values:
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _geometry_bbox(geometry: dict[str, Any] | None) -> list[float] | None:
    coords: list[tuple[float, float]] = []
    if geometry:
        _collect_positions(geometry.get("coordinates"), coords)
    if not coords:
        return None
    lons = [coord[0] for coord in coords]
    lats = [coord[1] for coord in coords]
    return [min(lons), min(lats), max(lons), max(lats)]


def _collect_positions(value: Any, coords: list[tuple[float, float]]) -> None:
    if not isinstance(value, list):
        return
    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        coords.append((float(value[0]), float(value[1])))
        return
    for item in value:
        _collect_positions(item, coords)


def _merge_bounds(bounds: list[list[float]]) -> list[float] | None:
    if not bounds:
        return None
    return [
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    ]
