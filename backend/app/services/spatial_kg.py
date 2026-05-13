import json
import math
import re
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from bson import ObjectId
from neo4j import Driver

from app.db.mongo import get_db

ProgressLogger = Callable[[str, int], Awaitable[None]]

GIS_CATEGORY_TYPES = {
    "area": {"name": "行政区", "label": "Area", "collection": "行政区_集合"},
    "build": {"name": "建筑", "label": "Building", "collection": "建筑_集合"},
    "traffic": {"name": "交通", "label": "Traffic", "collection": "交通_集合"},
    "water": {"name": "水域", "label": "Water", "collection": "水域_集合"},
    "other": {"name": "其他", "label": "OtherGISFeature", "collection": "其他_集合"},
}
INSAR_TYPE = "InSAR沉降监测点"
NAME_FIELD_PRIORITY = ["name", "NAME", "Name", "编码", "id", "ID"]
KRIGING_CONFIG = {"neighbor_k": 128, "min_weight_threshold": 0.1, "bandwidth": 0.003}
LARGE_ENTITY_THRESHOLD = {"LineString": 5000, "Polygon": 1_000_000}
LARGE_ENTITY_ANCHOR_NUM = {"LineString": 3, "Polygon": 4}


async def build_spatial_kg_from_mongo(dataset_id: str, log: ProgressLogger | None = None) -> dict[str, Any]:
    await _log(log, "读取 MongoDB 中的 GIS 与 InSAR 数据", 5)
    gis_docs = [
        document
        async for document in get_db()
        .gis_features.find({"dataset_id": dataset_id})
        .sort([("gis_category", 1), ("source_file_id", 1), ("feature_index", 1)])
    ]
    insar_records = [
        document
        async for document in get_db()
        .tabular_records.find({"dataset_id": dataset_id, "data_type": "insar"}).sort("row_number", 1)
    ]

    await _log(log, f"读取完成：GIS 要素 {len(gis_docs)} 个，InSAR 记录 {len(insar_records)} 条", 12)
    kg = _build_base_space_kg(dataset_id, gis_docs)
    await _log(log, f"基础空间图谱生成完成：{len(kg['entities'])} 个实体，{len(kg['relations'])} 条关系", 35)

    kg = _add_anchors_to_kg(kg)
    await _log(log, "实体空间锚点计算完成", 45)

    insar_entities = _records_to_insar_entities(dataset_id, insar_records)
    kg["entities"].extend(insar_entities)
    await _log(log, f"InSAR 点转换完成：{len(insar_entities)} 个有效点", 55)

    _append_spatial_relations(kg, include_insar=True)
    await _log(log, f"空间关系补充完成：累计 {len(kg['relations'])} 条关系", 70)

    kriging_relations = _calculate_kriging_relations(insar_entities)
    kg["relations"].extend(kriging_relations)
    kg["meta"]["insar_point_count"] = len(insar_entities)
    kg["meta"]["kriging_relation_count"] = len(kriging_relations)
    await _log(log, f"克里金空间影响关系完成：{len(kriging_relations)} 条", 82)

    return kg


def write_kg_to_neo4j(driver: Driver, dataset_id: str, kg: dict[str, Any]) -> dict[str, int]:
    with driver.session() as session:
        session.execute_write(_clear_dataset_graph, dataset_id)
        session.execute_write(_merge_dataset_root, dataset_id, kg.get("meta", {}))
        for entity in kg["entities"]:
            session.execute_write(_merge_entity, dataset_id, entity)
        for relation in kg["relations"]:
            session.execute_write(_merge_relation, dataset_id, relation)

    return {
        "entities": len(kg["entities"]),
        "relations": len(kg["relations"]),
        "gis_entities": len([entity for entity in kg["entities"] if entity.get("source_kind") == "gis"]),
        "insar_points": kg.get("meta", {}).get("insar_point_count", 0),
        "kriging_relations": kg.get("meta", {}).get("kriging_relation_count", 0),
    }


def _build_base_space_kg(dataset_id: str, gis_docs: list[dict[str, Any]]) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    area_entries: list[dict[str, Any]] = []
    category_geometries: dict[str, list[Any]] = defaultdict(list)
    collection_ids: dict[str, str] = {}

    area_docs = [document for document in gis_docs if document.get("gis_category") == "area"]
    other_docs = [document for document in gis_docs if document.get("gis_category") != "area"]

    for document in area_docs:
        entity = _gis_document_to_entity(dataset_id, document)
        entities.append(entity)
        geometry = _shape_or_none(entity.get("geometry"))
        if geometry is not None:
            area_entries.append({"entity_id": entity["__id__"], "entity_name": entity["entity_name"], "geometry": geometry})

    feature_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    sequence_by_category: dict[str, int] = defaultdict(int)
    for document in other_docs:
        entity = _gis_document_to_entity(dataset_id, document)
        geometry = _shape_or_none(entity.get("geometry"))
        admin_name, admin_id = _admin_area_info(geometry, area_entries)
        entity["admin_belong"] = admin_name
        entity["admin_entity_id"] = admin_id
        if not _has_explicit_name(entity["attributes"]):
            category = entity["type"]
            sequence_by_category[f"{admin_name}:{category}"] += 1
            entity["entity_name"] = f"{admin_name}_{category}_{sequence_by_category[f'{admin_name}:{category}']}"
        feature_groups[(entity["entity_name"], entity["type"])].append(entity)

    for (_, _), group in feature_groups.items():
        entity = _merge_entity_group(group)
        entities.append(entity)
        collection_key = f"{entity.get('admin_belong', '未知区域')}_{entity['type']}"
        collection_id = collection_ids.get(collection_key)
        if collection_id is None:
            collection_id = _make_id("collection", dataset_id, collection_key)
            collection_ids[collection_key] = collection_id
            collection_entity = {
                "__id__": collection_id,
                "entity_name": collection_key,
                "type": f"{entity['type']}_集合",
                "geom_type": "Collection",
                "geometry": None,
                "attributes": {"admin_name": entity.get("admin_belong"), "category_type": entity["type"]},
                "dataset_id": dataset_id,
                "source_kind": "gis_collection",
            }
            entities.append(collection_entity)
            if entity.get("admin_entity_id"):
                relations.append(
                    _relation(
                        entity["admin_entity_id"],
                        collection_id,
                        "包含",
                        "行政区-分类集合关系",
                        source_id=entity.get("source_file_id"),
                    )
                )
        relations.append(_relation(collection_id, entity["__id__"], "包含", "分类集合-要素关系"))
        geometry = _shape_or_none(entity.get("geometry"))
        if geometry is not None:
            category_geometries[collection_key].append(geometry)

    _attach_collection_geometry(entities, category_geometries)

    kg = {
        "entities": entities,
        "relations": relations,
        "meta": {
            "dataset_id": dataset_id,
            "generated_at": datetime.utcnow().isoformat(),
            "gis_feature_count": len(gis_docs),
            "area_feature_count": len(area_docs),
        },
    }
    _append_spatial_relations(kg, include_insar=False)
    return kg


def _gis_document_to_entity(dataset_id: str, document: dict[str, Any]) -> dict[str, Any]:
    category = document.get("gis_category") or "other"
    config = GIS_CATEGORY_TYPES.get(category, GIS_CATEGORY_TYPES["other"])
    geometry = document.get("geometry")
    properties = document.get("properties") or {}
    entity_name = _extract_entity_name(properties, document.get("layer_name"), document.get("feature_index"))
    return {
        "__id__": _make_id("gis", dataset_id, str(document["_id"])),
        "__created_at__": _timestamp(),
        "entity_name": entity_name,
        "type": document.get("gis_category_name") or config["name"],
        "geom_type": _geom_type_from_geojson(geometry),
        "geometry": geometry,
        "attributes": properties,
        "dataset_id": dataset_id,
        "mongo_id": str(document["_id"]),
        "source_file_id": document.get("source_file_id"),
        "source_file": document.get("layer_name"),
        "source_kind": "gis",
        "gis_category": category,
    }


def _records_to_insar_entities(dataset_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen_points: set[str] = set()
    for index, record in enumerate(records, start=1):
        normalized = record.get("normalized_fields") or {}
        raw = record.get("raw_fields") or {}
        lon = _first_number(normalized.get("longitude"), raw.get("lon"), raw.get("longitude"), raw.get("lng"), raw.get("经度"))
        lat = _first_number(normalized.get("latitude"), raw.get("lat"), raw.get("latitude"), raw.get("纬度"))
        if lon is None or lat is None:
            continue
        point_id = normalized.get("point_id") or raw.get("point_id") or raw.get("id") or str(record["_id"])
        unique_key = str(point_id)
        if unique_key in seen_points:
            unique_key = f"{point_id}_{record['_id']}"
        seen_points.add(unique_key)
        velocity = _first_number(normalized.get("velocity"), raw.get("velocity"), raw.get("rate"), raw.get("速率"))
        observations = {
            key: float(value)
            for key, value in raw.items()
            if re.match(r"^D_\d{8}$", str(key)) and _is_number(value)
        }
        entity_id = _make_id("insar", dataset_id, unique_key)
        entities.append(
            {
                "__id__": entity_id,
                "__created_at__": _timestamp(),
                "entity_name": f"InSAR监测点_{point_id or index}",
                "type": INSAR_TYPE,
                "geom_type": "Point",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "attributes": {
                    "point_id": str(point_id),
                    "point_index": index,
                    "base_attributes": raw,
                    "normalized_fields": normalized,
                    "velocity": velocity if velocity is not None else 0,
                    "insar_observations": observations,
                    "total_observations": len(observations),
                },
                "dataset_id": dataset_id,
                "mongo_id": str(record["_id"]),
                "source_file_id": record.get("source_file_id"),
                "source_kind": "insar",
            }
        )
    return entities


def _append_spatial_relations(kg: dict[str, Any], include_insar: bool) -> None:
    shape_rows: list[dict[str, Any]] = []
    existing_keys = {
        (relation.get("src_id"), relation.get("tgt_id"), relation.get("type"), relation.get("content"))
        for relation in kg["relations"]
    }
    for entity in kg["entities"]:
        if entity.get("type") == "行政区" or "集合" in str(entity.get("type")):
            continue
        if entity.get("source_kind") == "insar" and not include_insar:
            continue
        geometry = _shape_or_none(entity.get("geometry"))
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            continue
        shape_rows.append({"entity": entity, "geometry": geometry})

    for index, left in enumerate(shape_rows):
        for right in shape_rows[index + 1 :]:
            content = _spatial_relation_content(left["geometry"], right["geometry"])
            if not content:
                continue
            relation = _relation(left["entity"]["__id__"], right["entity"]["__id__"], content, "空间关系")
            key = (relation["src_id"], relation["tgt_id"], relation["type"], relation["content"])
            if key not in existing_keys:
                kg["relations"].append(relation)
                existing_keys.add(key)


def _calculate_kriging_relations(insar_entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(insar_entities) < 3:
        return []

    from scipy.spatial import KDTree
    import numpy as np

    coordinates = []
    values = []
    for entity in insar_entities:
        lon, lat = entity["geometry"]["coordinates"]
        coordinates.append([float(lon), float(lat)])
        values.append(float(entity.get("attributes", {}).get("velocity") or 0))

    coords = np.array(coordinates, dtype=np.float32)
    velocities = np.array(values, dtype=np.float32)
    tree = KDTree(coords)
    neighbor_count = min(len(insar_entities), KRIGING_CONFIG["neighbor_k"] + 1)
    bandwidth_squared = KRIGING_CONFIG["bandwidth"] ** 2
    relations: list[dict[str, Any]] = []

    for index, entity in enumerate(insar_entities):
        distances, indices = tree.query(coords[index], k=neighbor_count)
        distances = np.atleast_1d(distances)
        indices = np.atleast_1d(indices)
        for neighbor_index, distance in zip(indices[1:], distances[1:]):
            if int(neighbor_index) >= len(insar_entities):
                continue
            neighbor = insar_entities[int(neighbor_index)]
            weight = float(math.exp(-((float(distance) ** 2) / (2 * bandwidth_squared))))
            if abs(weight) < KRIGING_CONFIG["min_weight_threshold"]:
                continue
            relations.append(
                {
                    "__id__": _make_id("rel", entity["__id__"], neighbor["__id__"], f"{weight:.6f}"),
                    "__created_at__": _timestamp(),
                    "src_id": entity["__id__"],
                    "tgt_id": neighbor["__id__"],
                    "content": "空间影响",
                    "type": "空间影响关系",
                    "relation_name": f"{entity['entity_name']}影响{neighbor['entity_name']}",
                    "semantic_tag": f"InSAR沉降监测点-空间影响-权重{weight:.6f}",
                    "kriging_weight": round(weight, 6),
                    "source_velocity": round(float(velocities[index]), 3),
                    "target_velocity": round(float(velocities[int(neighbor_index)]), 3),
                    "is_nearest_neighbor": True,
                }
            )
    return relations


def _add_anchors_to_kg(kg: dict[str, Any]) -> dict[str, Any]:
    for entity in kg["entities"]:
        geometry = _shape_or_none(entity.get("geometry"))
        if geometry is None:
            entity["spatial_anchor"] = []
            entity["scale_level"] = _scale_level(entity)
            entity["semantic_tag"] = _semantic_tag(entity)
            continue
        anchors = _calculate_anchors(geometry, entity.get("geom_type") or _geom_type(geometry))
        bounds = [round(value, 6) for value in geometry.bounds]
        entity["spatial_anchor"] = [
            {
                "anchor_id": f"{entity['__id__']}_anchor_{index + 1}",
                "anchor_lon": lon,
                "anchor_lat": lat,
                "anchor_type": "main" if index == 0 else "sub",
                "geom_bounds": bounds,
            }
            for index, (lon, lat) in enumerate(anchors)
        ]
        entity["scale_level"] = _scale_level(entity)
        entity["semantic_tag"] = _semantic_tag(entity)
    kg["meta"]["anchor_attached"] = True
    return kg


def _merge_entity_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    if len(group) == 1:
        return group[0]
    geometries = [_shape_or_none(entity.get("geometry")) for entity in group]
    geometries = [geometry for geometry in geometries if geometry is not None and not geometry.is_empty]
    from shapely.ops import unary_union

    merged_geometry = unary_union(geometries) if geometries else None
    merged_attributes: dict[str, set[str]] = defaultdict(set)
    for entity in group:
        for key, value in (entity.get("attributes") or {}).items():
            if value not in (None, ""):
                merged_attributes[key].add(str(value))
    base = group[0].copy()
    base["__id__"] = _make_id("gis", base["dataset_id"], base["entity_name"], base["type"])
    base["geometry"] = merged_geometry.__geo_interface__ if merged_geometry is not None else None
    base["geom_type"] = _geom_type(merged_geometry) if merged_geometry is not None else "Unknown"
    base["attributes"] = {key: ",".join(sorted(values)) for key, values in merged_attributes.items()}
    base["merge_note"] = f"合并了 {len(group)} 个同名同类型实体"
    return base


def _attach_collection_geometry(entities: list[dict[str, Any]], category_geometries: dict[str, list[Any]]) -> None:
    if not category_geometries:
        return
    from shapely.ops import unary_union

    for entity in entities:
        if entity.get("entity_name") not in category_geometries:
            continue
        geometry = unary_union(category_geometries[entity["entity_name"]])
        entity["geometry"] = geometry.__geo_interface__
        entity["geom_type"] = _geom_type(geometry)
        entity["attributes"]["total_count"] = len(category_geometries[entity["entity_name"]])


def _merge_dataset_root(tx, dataset_id: str, meta: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (d:DatasetGraph:Entity {id: $id})
        SET d.name = $name,
            d.dataset_id = $dataset_id,
            d.entity_type = '数据集',
            d.meta_json = $meta_json
        """,
        id=f"dataset:{dataset_id}",
        name=f"数据集 {dataset_id}",
        dataset_id=dataset_id,
        meta_json=json.dumps(meta, ensure_ascii=False, default=str),
    )


def _merge_entity(tx, dataset_id: str, entity: dict[str, Any]) -> None:
    label = _neo4j_label(entity)
    props = _entity_props(dataset_id, entity)
    query = f"""
    MATCH (d:DatasetGraph {{id: $dataset_node_id}})
    MERGE (e:Entity:{label} {{id: $id}})
    SET e += $props
    MERGE (d)-[:HAS_ENTITY {{id: $dataset_rel_id}}]->(e)
    """
    tx.run(
        query,
        dataset_node_id=f"dataset:{dataset_id}",
        id=props["id"],
        props=props,
        dataset_rel_id=f"rel:dataset-entity:{dataset_id}:{props['id']}",
    )


def _merge_relation(tx, dataset_id: str, relation: dict[str, Any]) -> None:
    rel_type = _neo4j_relation_type(relation)
    props = _relation_props(dataset_id, relation)
    query = f"""
    MATCH (src:Entity {{id: $src_id}})
    MATCH (tgt:Entity {{id: $tgt_id}})
    MERGE (src)-[r:{rel_type} {{id: $id}}]->(tgt)
    SET r += $props
    """
    tx.run(query, src_id=relation["src_id"], tgt_id=relation["tgt_id"], id=props["id"], props=props)


def _clear_dataset_graph(tx, dataset_id: str) -> None:
    tx.run("MATCH (n {dataset_id: $dataset_id}) DETACH DELETE n", dataset_id=dataset_id)


def _entity_props(dataset_id: str, entity: dict[str, Any]) -> dict[str, Any]:
    centroid_lon = None
    centroid_lat = None
    anchors = entity.get("spatial_anchor") or []
    if anchors:
        centroid_lon = anchors[0].get("anchor_lon")
        centroid_lat = anchors[0].get("anchor_lat")
    return _clean_neo4j_props(
        {
            "id": entity["__id__"],
            "name": entity.get("entity_name") or entity["__id__"],
            "dataset_id": dataset_id,
            "entity_type": entity.get("type"),
            "geom_type": entity.get("geom_type"),
            "mongo_id": entity.get("mongo_id"),
            "source_file_id": entity.get("source_file_id"),
            "source_file": entity.get("source_file"),
            "source_kind": entity.get("source_kind"),
            "gis_category": entity.get("gis_category"),
            "admin_belong": entity.get("admin_belong"),
            "scale_level": entity.get("scale_level"),
            "semantic_tag": entity.get("semantic_tag"),
            "centroid_lon": centroid_lon,
            "centroid_lat": centroid_lat,
            "geometry_json": json.dumps(entity.get("geometry"), ensure_ascii=False, default=str),
            "attributes_json": json.dumps(entity.get("attributes") or {}, ensure_ascii=False, default=str),
            "spatial_anchor_json": json.dumps(entity.get("spatial_anchor") or [], ensure_ascii=False, default=str),
        }
    )


def _relation_props(dataset_id: str, relation: dict[str, Any]) -> dict[str, Any]:
    return _clean_neo4j_props(
        {
            "id": relation.get("__id__") or _make_id("rel", relation.get("src_id"), relation.get("tgt_id")),
            "dataset_id": dataset_id,
            "content": relation.get("content"),
            "relation_type": relation.get("type"),
            "relation_name": relation.get("relation_name"),
            "semantic_tag": relation.get("semantic_tag"),
            "source_id": relation.get("source_id"),
            "file_path": relation.get("file_path"),
            "kriging_weight": relation.get("kriging_weight"),
            "source_velocity": relation.get("source_velocity"),
            "target_velocity": relation.get("target_velocity"),
            "is_nearest_neighbor": relation.get("is_nearest_neighbor"),
        }
    )


def _clean_neo4j_props(props: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue
        clean[key] = value
    return clean


def _neo4j_label(entity: dict[str, Any]) -> str:
    entity_type = str(entity.get("type") or "")
    source_kind = entity.get("source_kind")
    category = entity.get("gis_category")
    if source_kind == "gis_collection" or "集合" in entity_type:
        return "GISCollection"
    if category in GIS_CATEGORY_TYPES:
        return GIS_CATEGORY_TYPES[category]["label"]
    if entity_type == INSAR_TYPE:
        return "InSARPoint"
    return "KnowledgeEntity"


def _neo4j_relation_type(relation: dict[str, Any]) -> str:
    if relation.get("type") == "空间影响关系":
        return "SPATIAL_INFLUENCE"
    content = relation.get("content")
    if content == "包含":
        return "CONTAINS"
    if content == "位于":
        return "LOCATED_IN"
    if content == "相交":
        return "INTERSECTS"
    return "RELATED_TO"


def _relation(src_id: str, tgt_id: str, content: str, relation_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "__id__": _make_id("rel", src_id, tgt_id, content, relation_type),
        "__created_at__": _timestamp(),
        "src_id": src_id,
        "tgt_id": tgt_id,
        "content": content,
        "type": relation_type,
        **extra,
    }


def _spatial_relation_content(left: Any, right: Any) -> str | None:
    if left.within(right):
        return "位于"
    if left.contains(right):
        return "包含"
    if left.intersects(right):
        return "相交"
    return None


def _admin_area_info(geometry: Any, areas: list[dict[str, Any]]) -> tuple[str, str | None]:
    if geometry is None or not areas:
        return "未知区域", None
    for area in areas:
        if area["geometry"].contains(geometry):
            return area["entity_name"], area["entity_id"]
    for area in areas:
        if area["geometry"].intersects(geometry):
            return area["entity_name"], area["entity_id"]
    return "未知区域", None


def _calculate_anchors(geometry: Any, geom_type: str) -> list[tuple[float, float]]:
    if geom_type == "Point":
        point = list(geometry.geoms)[0] if geometry.geom_type == "MultiPoint" else geometry
        return [(point.x, point.y)]
    if _is_large_entity(geometry, geom_type):
        if geom_type == "LineString":
            return _line_multi_anchors(geometry, LARGE_ENTITY_ANCHOR_NUM["LineString"])
        if geom_type == "Polygon":
            return _polygon_multi_anchors(geometry, LARGE_ENTITY_ANCHOR_NUM["Polygon"])
    representative = geometry.representative_point() if geom_type == "Polygon" else geometry.centroid
    return [(representative.x, representative.y)]


def _is_large_entity(geometry: Any, geom_type: str) -> bool:
    try:
        import geopandas as gpd

        projected = gpd.GeoSeries([geometry], crs="EPSG:4326").to_crs(epsg=32649).iloc[0]
        if geom_type == "LineString":
            return projected.length > LARGE_ENTITY_THRESHOLD["LineString"]
        if geom_type == "Polygon":
            return projected.area > LARGE_ENTITY_THRESHOLD["Polygon"]
    except Exception:
        return False
    return False


def _line_multi_anchors(geometry: Any, count: int) -> list[tuple[float, float]]:
    from shapely.ops import unary_union

    line = unary_union(geometry) if geometry.geom_type == "MultiLineString" else geometry
    return [(line.interpolate(index / (count + 1), normalized=True).x, line.interpolate(index / (count + 1), normalized=True).y) for index in range(1, count + 1)]


def _polygon_multi_anchors(geometry: Any, count: int) -> list[tuple[float, float]]:
    from shapely.geometry import box

    min_x, min_y, max_x, max_y = geometry.bounds
    mid_x = (min_x + max_x) / 2
    mid_y = (min_y + max_y) / 2
    grids = [box(min_x, min_y, mid_x, mid_y), box(mid_x, min_y, max_x, mid_y), box(min_x, mid_y, mid_x, max_y), box(mid_x, mid_y, max_x, max_y)]
    anchors: list[tuple[float, float]] = []
    for grid in grids[:count]:
        intersection = grid.intersection(geometry)
        if not intersection.is_empty:
            point = intersection.representative_point()
            anchors.append((point.x, point.y))
    return anchors or [(geometry.representative_point().x, geometry.representative_point().y)]


def _scale_level(entity: dict[str, Any]) -> int:
    entity_type = entity.get("type")
    if entity_type == "行政区":
        return 1
    if entity_type in {"交通", "水域"}:
        return 2
    if entity_type in {"建筑", INSAR_TYPE}:
        return 3
    return 2


def _semantic_tag(entity: dict[str, Any]) -> str:
    return f"{entity.get('type', '未知要素')}-{entity.get('entity_name', '未知')}"


def _extract_entity_name(properties: dict[str, Any], layer_name: str | None, feature_index: int | None) -> str:
    for field in NAME_FIELD_PRIORITY:
        value = properties.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return f"{layer_name or 'GIS要素'}_{feature_index or 1}"


def _has_explicit_name(properties: dict[str, Any]) -> bool:
    return any(properties.get(field) not in (None, "") for field in NAME_FIELD_PRIORITY)


def _shape_or_none(geometry: dict[str, Any] | None) -> Any:
    if not geometry:
        return None
    from shapely.geometry import shape

    try:
        return shape(geometry)
    except Exception:
        return None


def _geom_type_from_geojson(geometry: dict[str, Any] | None) -> str:
    shape_object = _shape_or_none(geometry)
    return _geom_type(shape_object) if shape_object is not None else "Unknown"


def _geom_type(geometry: Any) -> str:
    if geometry is None:
        return "Unknown"
    if geometry.geom_type in {"Point", "MultiPoint"}:
        return "Point"
    if geometry.geom_type in {"LineString", "MultiLineString"}:
        return "LineString"
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return "Polygon"
    return "Unknown"


def _first_number(*values: Any) -> float | None:
    for value in values:
        if _is_number(value):
            return float(value)
    return None


def _is_number(value: Any) -> bool:
    try:
        if value is None or value == "":
            return False
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _make_id(*parts: Any) -> str:
    text = ":".join(str(part) for part in parts if part is not None)
    return f"{parts[0]}:{uuid.uuid5(uuid.NAMESPACE_URL, text)}"


def _timestamp() -> float:
    return datetime.utcnow().timestamp()


async def _log(log: ProgressLogger | None, message: str, progress: int) -> None:
    if log is not None:
        await log(message, progress)
