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
from app.services.insar_time_series import strip_insar_time_fields, summarize_insar_raw_fields

ProgressLogger = Callable[[str, int], Awaitable[None]]

GIS_CATEGORY_TYPES = {
    "area": {"name": "\u884c\u653f\u533a", "label": "Area", "collection": "\u884c\u653f\u533a_\u96c6\u5408"},
    "build": {"name": "\u5efa\u7b51", "label": "Building", "collection": "\u5efa\u7b51_\u96c6\u5408"},
    "traffic": {"name": "\u4ea4\u901a", "label": "Traffic", "collection": "\u4ea4\u901a_\u96c6\u5408"},
    "water": {"name": "\u6c34\u57df", "label": "Water", "collection": "\u6c34\u57df_\u96c6\u5408"},
    "other": {"name": "\u5176\u4ed6", "label": "OtherGISFeature", "collection": "\u5176\u4ed6_\u96c6\u5408"},
}
INSAR_TYPE = "InSAR\u6c89\u964d\u76d1\u6d4b\u70b9"
NAME_FIELD_PRIORITY = ["name", "NAME", "Name", "\u7f16\u7801", "id", "ID"]
KRIGING_CONFIG = {"neighbor_k": 12, "min_weight_threshold": 0.1, "bandwidth": 0.003}
SPATIAL_RELATION_LIMIT = 20_000
KRIGING_RELATION_LIMIT = 20_000
LARGE_ENTITY_THRESHOLD = {"LineString": 5000, "Polygon": 1_000_000}
LARGE_ENTITY_ANCHOR_NUM = {"LineString": 3, "Polygon": 4}


async def build_spatial_kg_from_mongo(dataset_id: str, log: ProgressLogger | None = None) -> dict[str, Any]:
    await _log(log, "读取 MongoDB 中的 GIS 与 InSAR 数据", 5)
    dataset = await get_db().datasets.find_one({"_id": ObjectId(dataset_id)})
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
    kg = _build_base_space_kg(dataset_id, gis_docs, dataset.get("name") if dataset else dataset_id)
    await _log(log, f"基础空间图谱生成完成：{len(kg['entities'])} 个实体，{len(kg['relations'])} 条关系", 35)

    kg = _add_anchors_to_kg(kg)
    await _log(log, "实体空间锚点计算完成", 45)

    insar_entities = _records_to_insar_entities(dataset_id, insar_records)
    _attach_insar_collections(dataset_id, kg, insar_entities)
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


def _build_base_space_kg(dataset_id: str, gis_docs: list[dict[str, Any]], dataset_name: str) -> dict[str, Any]:
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
        if _is_weak_entity_name(entity["entity_name"]):
            category = entity["type"]
            sequence_by_category[f"{admin_name}:{category}"] += 1
            entity["entity_name"] = _fallback_entity_name(
                entity["attributes"],
                category,
                admin_name,
                sequence_by_category[f"{admin_name}:{category}"],
            )
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
            "dataset_name": dataset_name,
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
    entity_name = _extract_entity_name(properties, document.get("layer_name"), document.get("feature_index"), config["name"])
    return {
        "__id__": _make_id("gis", dataset_id, str(document["_id"])),
        "__created_at__": _timestamp(),
        "entity_name": entity_name,
        "type": config["name"],
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


def _attach_insar_collections(dataset_id: str, kg: dict[str, Any], insar_entities: list[dict[str, Any]]) -> None:
    if not insar_entities:
        return
    area_entries = _area_entries_from_kg(kg)
    collection_ids: dict[str, str] = {}
    collection_counts: dict[str, int] = defaultdict(int)

    for entity in insar_entities:
        geometry = _shape_or_none(entity.get("geometry"))
        admin_name, admin_id = _admin_area_info(geometry, area_entries)
        entity["admin_belong"] = admin_name
        entity["admin_entity_id"] = admin_id
        collection_key = f"{admin_name}_InSAR监测点集合"
        collection_id = collection_ids.get(collection_key)
        if collection_id is None:
            collection_id = _make_id("collection", dataset_id, collection_key)
            collection_ids[collection_key] = collection_id
            kg["entities"].append(
                {
                    "__id__": collection_id,
                    "__created_at__": _timestamp(),
                    "entity_name": collection_key,
                    "type": "InSAR监测点集合",
                    "geom_type": "Collection",
                    "geometry": None,
                    "attributes": {"admin_name": admin_name, "category_type": "InSAR监测点"},
                    "dataset_id": dataset_id,
                    "source_kind": "gis_collection",
                }
            )
            if admin_id:
                kg["relations"].append(
                    _relation(admin_id, collection_id, "包含", "行政区-分类集合关系")
                )
        collection_counts[collection_id] += 1
        kg["relations"].append(_relation(collection_id, entity["__id__"], "包含", "分类集合-要素关系"))

    for entity in kg["entities"]:
        if entity["__id__"] in collection_counts:
            entity.setdefault("attributes", {})["total_count"] = collection_counts[entity["__id__"]]


def _area_entries_from_kg(kg: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entity in kg.get("entities", []):
        if entity.get("source_kind") != "gis" or entity.get("gis_category") != "area":
            continue
        geometry = _shape_or_none(entity.get("geometry"))
        if geometry is not None:
            entries.append({"entity_id": entity["__id__"], "entity_name": entity["entity_name"], "geometry": geometry})
    return entries


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
        time_summary = summarize_insar_raw_fields(raw)
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
                    "base_attributes": strip_insar_time_fields(raw),
                    "normalized_fields": normalized,
                    "velocity": velocity if velocity is not None else 0,
                    "insar_observations": observations,
                    "total_observations": len(observations),
                    "start_date": time_summary.get("start_date"),
                    "end_date": time_summary.get("end_date"),
                    "observation_count": time_summary.get("observation_count"),
                    "latest_value": time_summary.get("latest_value"),
                    "max_settlement": time_summary.get("max_settlement"),
                    "max_uplift": time_summary.get("max_uplift"),
                    "cumulative_change": time_summary.get("cumulative_change"),
                    "average_rate": time_summary.get("average_rate"),
                    "trend": time_summary.get("trend"),
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

    if len(shape_rows) < 2:
        return

    from shapely.strtree import STRtree

    geometries = [row["geometry"] for row in shape_rows]
    tree = STRtree(geometries)
    relation_count = 0

    for index, left in enumerate(shape_rows):
        candidate_indexes = tree.query(left["geometry"])
        for candidate_index in candidate_indexes:
            right_index = int(candidate_index)
            if right_index <= index:
                continue
            right = shape_rows[right_index]
            content = _spatial_relation_content(left["geometry"], right["geometry"])
            if not content:
                continue
            relation = _relation(left["entity"]["__id__"], right["entity"]["__id__"], content, "空间关系")
            key = (relation["src_id"], relation["tgt_id"], relation["type"], relation["content"])
            if key not in existing_keys:
                kg["relations"].append(relation)
                existing_keys.add(key)
                relation_count += 1
                if relation_count >= SPATIAL_RELATION_LIMIT:
                    kg["meta"]["spatial_relation_limited"] = True
                    kg["meta"]["spatial_relation_limit"] = SPATIAL_RELATION_LIMIT
                    return


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
            if len(relations) >= KRIGING_RELATION_LIMIT:
                return relations
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
            d.meta_json = $meta_json,
            d.text_facts_json = $text_facts_json,
            d.text_fact_count = $text_fact_count
        """,
        id=f"dataset:{dataset_id}",
        name=meta.get("dataset_name") or f"数据集 {dataset_id}",
        dataset_id=dataset_id,
        meta_json=json.dumps(meta, ensure_ascii=False, default=str),
        text_facts_json=json.dumps(meta.get("text_facts") or [], ensure_ascii=False, default=str),
        text_fact_count=meta.get("text_fact_count") or len(meta.get("text_facts") or []),
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
    attributes = entity.get("attributes") or {}
    region_id = entity.get("region_id")
    region_name = entity.get("region_name")
    if isinstance(attributes, dict):
        region_id = region_id or attributes.get("region_id")
        region_name = region_name or attributes.get("region_name")
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
            "region_id": region_id,
            "region_name": region_name,
            "scale_level": entity.get("scale_level"),
            "semantic_tag": entity.get("semantic_tag"),
            "centroid_lon": centroid_lon,
            "centroid_lat": centroid_lat,
            "start_date": attributes.get("start_date") if isinstance(attributes, dict) else None,
            "end_date": attributes.get("end_date") if isinstance(attributes, dict) else None,
            "observation_count": attributes.get("observation_count") if isinstance(attributes, dict) else None,
            "latest_value": attributes.get("latest_value") if isinstance(attributes, dict) else None,
            "max_settlement": attributes.get("max_settlement") if isinstance(attributes, dict) else None,
            "average_rate": attributes.get("average_rate") if isinstance(attributes, dict) else None,
            "trend": attributes.get("trend") if isinstance(attributes, dict) else None,
            "geometry_json": json.dumps(entity.get("geometry"), ensure_ascii=False, default=str),
            "attributes_json": json.dumps(attributes, ensure_ascii=False, default=str),
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
            "subject": relation.get("subject"),
            "object": relation.get("object"),
            "time": relation.get("time"),
            "location": relation.get("location"),
            "region_id": relation.get("region_id"),
            "region_name": relation.get("region_name"),
            "confidence": relation.get("confidence"),
            "evidence_text": relation.get("evidence_text"),
            "chunk_id": relation.get("chunk_id"),
            "document_id": relation.get("document_id"),
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
    if source_kind == "text_collection":
        return "TextKnowledgeCollection"
    if source_kind == "text_entity":
        if entity_type in {"TextEvent", "RiskFactor", "EngineeringMeasure"}:
            return entity_type
        return "TextEntity"
    if source_kind == "document":
        return "Document"
    if source_kind == "document_chunk":
        return "DocumentChunk"
    if source_kind == "gis_collection" or "集合" in entity_type:
        return "GISCollection"
    if category in GIS_CATEGORY_TYPES:
        return GIS_CATEGORY_TYPES[category]["label"]
    if entity_type == INSAR_TYPE:
        return "InSARPoint"
    return "KnowledgeEntity"


def _neo4j_relation_type(relation: dict[str, Any]) -> str:
    relation_type = relation.get("type")
    if relation_type == "空间影响关系":
        return "SPATIAL_INFLUENCE"
    content = relation.get("content")
    if relation_type in {"区域-文本知识集合关系", "文本知识集合-实体关系"} or content == "文本知识":
        return "HAS_TEXT_KNOWLEDGE"
    if relation_type == "实体-文本切片关系" or content == "提及":
        return "MENTIONED_IN"
    if relation_type == "文本五元组关系":
        text = str(content or relation.get("relation_name") or "")
        if any(word in text for word in ("诱发", "导致", "引起", "加剧")):
            return "CAUSES"
        if "影响" in text:
            return "AFFECTS"
        if "风险" in text:
            return "HAS_RISK"
        if any(word in text for word in ("治理", "防治", "控制", "减缓")):
            return "CONTROLLED_BY"
        return "RELATED_TO"
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


def _extract_entity_name(
    properties: dict[str, Any],
    layer_name: str | None,
    feature_index: int | None,
    entity_type: str | None = None,
) -> str:
    for field in _name_fields(properties):
        value = properties.get(field)
        name = _clean_name_value(value, entity_type)
        if name:
            return name
    return f"{layer_name or 'GIS要素'}_{feature_index or 1}"


def _has_explicit_name(properties: dict[str, Any]) -> bool:
    return any(_clean_name_value(properties.get(field)) for field in _name_fields(properties))


def _name_fields(properties: dict[str, Any]) -> list[str]:
    dynamic = sorted(
        [key for key in properties if "name" in str(key).lower() or str(key) in {"名称", "名字", "地名"}],
        key=lambda key: (0 if str(key).lower() == "name" else 1, str(key)),
    )
    return list(dict.fromkeys(NAME_FIELD_PRIORITY + dynamic))


def _clean_name_value(value: Any, entity_type: str | None = None) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or _is_weak_entity_name(text):
        return None
    parts = [part.strip() for part in re.split(r"[,，;；/、]", text) if part.strip()]
    parts = [part for part in parts if not _is_weak_entity_name(part)]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if entity_type:
        return f"{parts[0]}等{len(parts)}处{entity_type}"
    return "、".join(parts[:4])


def _is_weak_entity_name(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    compact = re.sub(r"[\s,，;；/、._-]+", "", text)
    return compact.isdigit() or bool(re.fullmatch(r"[0-9A-Fa-f]{8,}", compact))


def _fallback_entity_name(properties: dict[str, Any], entity_type: str, admin_name: str, sequence: int) -> str:
    for field in ("name_1", "name_2", "name", "NAME", "Name"):
        name = _clean_name_value(properties.get(field), entity_type)
        if name:
            return name if entity_type in name else f"{name}_{entity_type}"
    base = admin_name if admin_name and admin_name != "未知区域" else "未知区域"
    return f"{base}_{entity_type}_{sequence}"


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
