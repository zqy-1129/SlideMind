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
AREA_NAME_FIELDS = ("name", "NAME", "Name", "名称", "name_1", "name_2")
FEATURE_NAME_FIELDS = ("name", "NAME", "Name", "名称", "name_1")


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
    area_entries = await _load_area_entries(dataset_id)
    area_color_indexes = _assign_area_color_indexes(area_entries)

    for category, (layer_key, category_name) in GIS_LAYER_CONFIG.items():
        query = {"dataset_id": dataset_id, "gis_category": category}
        total = await get_db().gis_features.count_documents(query)
        limit = total if category == "area" else safe_limit
        cursor = get_db().gis_features.find(query).sort("feature_index", 1).limit(limit)
        sequence_by_region: dict[str, int] = {}
        features = []
        async for document in cursor:
            admin_name = _admin_name_for_document(document, area_entries)
            sequence_key = f"{admin_name}:{category}"
            sequence_by_region[sequence_key] = sequence_by_region.get(sequence_key, 0) + 1
            feature = _gis_feature(
                document,
                category,
                category_name,
                simplify,
                admin_name,
                sequence_by_region[sequence_key],
                area_color_indexes.get(str(document["_id"])),
            )
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


async def _load_area_entries(dataset_id: str) -> list[dict[str, Any]]:
    entries = []
    cursor = get_db().gis_features.find({"dataset_id": dataset_id, "gis_category": "area"}).sort("feature_index", 1)
    async for document in cursor:
        geometry = document.get("geometry")
        if not isinstance(geometry, dict):
            continue
        entries.append(
            {
                "id": str(document["_id"]),
                "name": _explicit_name(document.get("properties") or {}, AREA_NAME_FIELDS) or "未知区域",
                "geometry": geometry,
                "bbox": document.get("bbox") or _geometry_bbox(geometry),
            }
        )
    return entries


def _assign_area_color_indexes(area_entries: list[dict[str, Any]]) -> dict[str, int]:
    if not area_entries:
        return {}
    try:
        from shapely.geometry import shape

        geometries = []
        for entry in area_entries:
            geometries.append(shape(entry["geometry"]))
        neighbors: dict[int, set[int]] = {index: set() for index in range(len(area_entries))}
        for index, geom_a in enumerate(geometries):
            bbox_a = area_entries[index].get("bbox")
            if not bbox_a:
                continue
            for other_index in range(index + 1, len(geometries)):
                bbox_b = area_entries[other_index].get("bbox")
                if not bbox_b or not _bbox_intersects(bbox_a, bbox_b):
                    continue
                geom_b = geometries[other_index]
                if geom_a.touches(geom_b) or geom_a.intersects(geom_b):
                    neighbors[index].add(other_index)
                    neighbors[other_index].add(index)
        color_indexes: dict[int, int] = {}
        order = sorted(range(len(area_entries)), key=lambda item: len(neighbors[item]), reverse=True)
        for index in order:
            used = {color_indexes[neighbor] for neighbor in neighbors[index] if neighbor in color_indexes}
            color_index = 0
            while color_index in used:
                color_index += 1
            color_indexes[index] = color_index
        return {area_entries[index]["id"]: color for index, color in color_indexes.items()}
    except Exception:
        return {entry["id"]: index for index, entry in enumerate(area_entries)}


def _bbox_intersects(left: list[float], right: list[float]) -> bool:
    return not (left[2] < right[0] or right[2] < left[0] or left[3] < right[1] or right[3] < left[1])


def _feature_collection(features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features or []}


def _gis_feature(
    document: dict[str, Any],
    category: str,
    category_name: str,
    simplify: bool,
    admin_name: str,
    sequence: int,
    area_color_index: int | None = None,
) -> dict[str, Any] | None:
    geometry = document.get("geometry")
    if not isinstance(geometry, dict):
        return None
    display_geometry = _simplify_geometry(geometry) if simplify else geometry
    properties = document.get("properties") or {}
    explicit_name = _explicit_name(properties, AREA_NAME_FIELDS if category == "area" else FEATURE_NAME_FIELDS)
    name = explicit_name or f"{admin_name}_{category_name}_{sequence}"
    return {
        "type": "Feature",
        "geometry": display_geometry,
        "properties": {
            "id": str(document["_id"]),
            "name": name,
            "admin_name": admin_name,
            "layer_type": category,
            "layer_type_name": category_name,
            "geometry_type": document.get("geometry_type") or geometry.get("type"),
            "source_file_id": document.get("source_file_id"),
            "layer_name": document.get("layer_name"),
            "centroid": document.get("centroid"),
            "bbox": document.get("bbox"),
            "area_color_index": area_color_index,
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
    lon, lat = _normalize_lng_lat(lon, lat)
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


def _normalize_lng_lat(lon: float, lat: float) -> tuple[float, float]:
    if -90 <= lon <= 90 and 90 <= lat <= 180:
        return lat, lon
    return lon, lat


def _explicit_name(properties: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = properties.get(field)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text and not _weak_name(text):
            return text
    return None


def _weak_name(value: str) -> bool:
    compact = value.replace(",", "").replace("，", "").replace("_", "").replace("-", "").strip()
    return compact.isdigit() or (len(compact) >= 16 and all(char in "0123456789abcdefABCDEF" for char in compact))


def _admin_name_for_document(document: dict[str, Any], areas: list[dict[str, Any]]) -> str:
    properties = document.get("properties") or {}
    explicit = properties.get("admin_belong") or properties.get("name_2")
    if explicit not in (None, ""):
        return str(explicit).strip()
    if document.get("gis_category") == "area":
        return _explicit_name(properties, AREA_NAME_FIELDS) or "未知区域"

    centroid = document.get("centroid") or {}
    lon = _first_number(centroid.get("longitude"))
    lat = _first_number(centroid.get("latitude"))
    if lon is None or lat is None:
        return "未知区域"
    for area in areas:
        bbox = area.get("bbox")
        if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        if _point_in_geometry(lon, lat, area["geometry"]):
            return area["name"]
    return "未知区域"


def _point_in_geometry(lon: float, lat: float, geometry: dict[str, Any]) -> bool:
    try:
        from shapely.geometry import Point, shape

        return shape(geometry).contains(Point(lon, lat))
    except Exception:
        return False


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
