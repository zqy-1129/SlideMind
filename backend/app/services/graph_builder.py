import json
from typing import Any

from neo4j import Driver

from app.db.mongo import get_db
from app.db.neo4j import get_driver

GIS_CATEGORY_CONFIG = {
    "area": {"label": "Area", "name": "行政区"},
    "build": {"label": "Building", "name": "建筑"},
    "traffic": {"label": "Traffic", "name": "交通"},
    "water": {"label": "Water", "name": "水域"},
    "other": {"label": "OtherGISFeature", "name": "其他"},
}
GIS_NAME_FIELDS = ["name", "NAME", "Name", "编码", "id", "ID"]


async def build_graph_for_dataset(dataset_id: str) -> dict[str, int]:
    database = get_db()
    driver = get_driver()

    landslides: set[str] = set()
    monitor_points: dict[str, dict[str, Any]] = {}
    reservoir_stations: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    records = database.tabular_records.find({"dataset_id": dataset_id})
    async for record in records:
        normalized = record.get("normalized_fields", {})
        landslide_name = normalized.get("landslide_name") or "未命名滑坡"
        landslides.add(landslide_name)

        point_id = normalized.get("point_id")
        if point_id:
            monitor_points[point_id] = {
                "id": f"monitor:{dataset_id}:{point_id}",
                "name": str(point_id),
                "landslide_name": landslide_name,
                "mongo_id": str(record["_id"]),
                "dataset_id": dataset_id,
                "longitude": normalized.get("longitude"),
                "latitude": normalized.get("latitude"),
            }

        station_name = normalized.get("station_name")
        if station_name:
            reservoir_stations[station_name] = {
                "id": f"station:{dataset_id}:{station_name}",
                "name": str(station_name),
                "landslide_name": landslide_name,
                "mongo_id": str(record["_id"]),
                "dataset_id": dataset_id,
            }

        displacement = normalized.get("displacement")
        if isinstance(displacement, (int, float)) and abs(displacement) >= 30:
            event_id = f"event:{dataset_id}:{record['_id']}"
            events.append(
                {
                    "id": event_id,
                    "name": "位移异常",
                    "dataset_id": dataset_id,
                    "mongo_id": str(record["_id"]),
                    "landslide_name": landslide_name,
                    "point_id": point_id,
                    "value": displacement,
                }
            )

    gis_features: list[dict[str, Any]] = []
    cursor = database.gis_features.find({"dataset_id": dataset_id})
    async for feature in cursor:
        gis_features.append(_feature_to_graph_props(feature))

    with driver.session() as session:
        session.execute_write(_clear_dataset_graph, dataset_id)
        session.execute_write(_merge_dataset_root, dataset_id)
        for landslide_name in landslides:
            session.execute_write(_merge_landslide, dataset_id, landslide_name)
        for point in monitor_points.values():
            session.execute_write(_merge_monitor_point, point)
        for station in reservoir_stations.values():
            session.execute_write(_merge_reservoir_station, station)
        for event in events:
            session.execute_write(_merge_event, event)
        for category in sorted({feature["gis_category"] for feature in gis_features}):
            session.execute_write(_merge_gis_collection, dataset_id, category)
        for feature in gis_features:
            session.execute_write(_merge_gis_feature, feature)

    return {
        "landslides": len(landslides),
        "monitor_points": len(monitor_points),
        "reservoir_stations": len(reservoir_stations),
        "events": len(events),
        "gis_features": len(gis_features),
        "gis_categories": len({feature["gis_category"] for feature in gis_features}),
    }


def read_graph(dataset_id: str | None = None, limit: int = 120) -> dict[str, list[dict[str, Any]]]:
    driver = get_driver()
    dataset_filter = "WHERE n.dataset_id = $dataset_id OR m.dataset_id = $dataset_id" if dataset_id else ""
    query = f"""
    MATCH (n)-[r]->(m)
    {dataset_filter}
    RETURN n, r, m
    LIMIT $limit
    """

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    with driver.session() as session:
        result = session.run(query, dataset_id=dataset_id, limit=limit)
        for row in result:
            for key in ("n", "m"):
                node = row[key]
                node_id = node.get("id") or str(node.element_id)
                labels = list(node.labels)
                nodes[node_id] = {
                    "id": node_id,
                    "label": node.get("name") or node_id,
                    "type": node.get("entity_type") or (labels[0] if labels else "Entity"),
                    "properties": dict(node),
                }
            rel = row["r"]
            edge_id = rel.get("id") or str(rel.element_id)
            edges[edge_id] = {
                "id": edge_id,
                "source": row["n"].get("id") or str(row["n"].element_id),
                "target": row["m"].get("id") or str(row["m"].element_id),
                "label": rel.type,
                "properties": dict(rel),
            }

    if not nodes:
        return _read_orphan_nodes(driver, dataset_id, limit)
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _read_orphan_nodes(driver: Driver, dataset_id: str | None, limit: int) -> dict[str, list[dict[str, Any]]]:
    query = "MATCH (n) WHERE $dataset_id IS NULL OR n.dataset_id = $dataset_id RETURN n LIMIT $limit"
    with driver.session() as session:
        result = session.run(query, dataset_id=dataset_id, limit=limit)
        nodes = []
        for row in result:
            node = row["n"]
            node_id = node.get("id") or str(node.element_id)
            labels = list(node.labels)
            nodes.append(
                {
                    "id": node_id,
                    "label": node.get("name") or node_id,
                    "type": node.get("entity_type") or (labels[0] if labels else "Entity"),
                    "properties": dict(node),
                }
            )
    return {"nodes": nodes, "edges": []}


def _clear_dataset_graph(tx, dataset_id: str) -> None:
    tx.run("MATCH (n {dataset_id: $dataset_id}) DETACH DELETE n", dataset_id=dataset_id)


def _merge_dataset_root(tx, dataset_id: str) -> None:
    tx.run(
        """
        MERGE (d:DatasetGraph:Entity {id: $id})
        SET d.name = $name, d.dataset_id = $dataset_id, d.entity_type = '数据集'
        """,
        id=f"dataset:{dataset_id}",
        name=f"数据集 {dataset_id}",
        dataset_id=dataset_id,
    )


def _merge_landslide(tx, dataset_id: str, name: str) -> None:
    tx.run(
        """
        MATCH (d:DatasetGraph {id: $dataset_id_node})
        MERGE (l:Landslide:Entity {id: $id})
        SET l.name = $name, l.dataset_id = $dataset_id, l.entity_type = '滑坡体'
        MERGE (d)-[:HAS_LANDSLIDE {id: $rel_id}]->(l)
        """,
        dataset_id_node=f"dataset:{dataset_id}",
        id=f"landslide:{dataset_id}:{name}",
        name=name,
        dataset_id=dataset_id,
        rel_id=f"rel:dataset-landslide:{dataset_id}:{name}",
    )


def _merge_monitor_point(tx, point: dict[str, Any]) -> None:
    tx.run(
        """
        MATCH (l:Landslide {id: $landslide_id})
        MERGE (p:MonitorPoint:InSARPoint:Entity {id: $id})
        SET p += $props, p.entity_type = 'InSAR监测点'
        MERGE (l)-[:MONITORS {id: $rel_id}]->(p)
        """,
        landslide_id=f"landslide:{point['dataset_id']}:{point['landslide_name']}",
        id=point["id"],
        props=point,
        rel_id=f"rel:monitors:{point['dataset_id']}:{point['name']}",
    )


def _merge_reservoir_station(tx, station: dict[str, Any]) -> None:
    tx.run(
        """
        MATCH (l:Landslide {id: $landslide_id})
        MERGE (s:ReservoirStation:Entity {id: $id})
        SET s += $props, s.entity_type = '库水位站'
        MERGE (s)-[:AFFECTS {id: $rel_id}]->(l)
        """,
        landslide_id=f"landslide:{station['dataset_id']}:{station['landslide_name']}",
        id=station["id"],
        props=station,
        rel_id=f"rel:affects:{station['dataset_id']}:{station['name']}",
    )


def _merge_event(tx, event: dict[str, Any]) -> None:
    tx.run(
        """
        MATCH (l:Landslide {id: $landslide_id})
        MERGE (e:DisplacementEvent:Entity {id: $id})
        SET e += $props, e.entity_type = '位移事件'
        MERGE (l)-[:HAS_EVENT {id: $rel_id}]->(e)
        """,
        landslide_id=f"landslide:{event['dataset_id']}:{event['landslide_name']}",
        id=event["id"],
        props=event,
        rel_id=f"rel:event:{event['id']}",
    )


def _merge_gis_collection(tx, dataset_id: str, category: str) -> None:
    config = GIS_CATEGORY_CONFIG.get(category, GIS_CATEGORY_CONFIG["other"])
    collection_id = f"gis-collection:{dataset_id}:{category}"
    tx.run(
        """
        MATCH (d:DatasetGraph {id: $dataset_id_node})
        MERGE (c:GISCollection:Entity {id: $id})
        SET c.name = $name,
            c.dataset_id = $dataset_id,
            c.gis_category = $category,
            c.entity_type = $entity_type
        MERGE (d)-[:HAS_GIS_LAYER {id: $rel_id}]->(c)
        """,
        dataset_id_node=f"dataset:{dataset_id}",
        id=collection_id,
        name=config["name"],
        dataset_id=dataset_id,
        category=category,
        entity_type=f"{config['name']}_集合",
        rel_id=f"rel:dataset-gis-layer:{dataset_id}:{category}",
    )


def _merge_gis_feature(tx, feature: dict[str, Any]) -> None:
    label = GIS_CATEGORY_CONFIG.get(feature["gis_category"], GIS_CATEGORY_CONFIG["other"])["label"]
    query = f"""
    MATCH (c:GISCollection {{id: $collection_id}})
    MERGE (g:GISFeature:{label}:Entity {{id: $id}})
    SET g += $props
    MERGE (c)-[:CONTAINS {{id: $rel_id}}]->(g)
    """
    tx.run(
        query,
        collection_id=f"gis-collection:{feature['dataset_id']}:{feature['gis_category']}",
        id=feature["id"],
        props=feature,
        rel_id=f"rel:gis-contains:{feature['dataset_id']}:{feature['mongo_id']}",
    )


def _feature_to_graph_props(feature: dict[str, Any]) -> dict[str, Any]:
    category = feature.get("gis_category") or "other"
    config = GIS_CATEGORY_CONFIG.get(category, GIS_CATEGORY_CONFIG["other"])
    properties = feature.get("properties") or {}
    centroid = feature.get("centroid") or {}
    return {
        "id": f"gis:{feature['dataset_id']}:{feature['_id']}",
        "name": _extract_gis_name(properties, feature.get("layer_name"), feature.get("feature_index")),
        "dataset_id": feature["dataset_id"],
        "mongo_id": str(feature["_id"]),
        "source_file_id": feature.get("source_file_id"),
        "feature_index": feature.get("feature_index"),
        "layer_name": feature.get("layer_name"),
        "gis_category": category,
        "gis_category_name": feature.get("gis_category_name") or config["name"],
        "entity_type": feature.get("gis_category_name") or config["name"],
        "geometry_type": feature.get("geometry_type"),
        "centroid_lon": centroid.get("longitude"),
        "centroid_lat": centroid.get("latitude"),
        "bbox": feature.get("bbox"),
        "properties_json": json.dumps(properties, ensure_ascii=False),
    }


def _extract_gis_name(properties: dict[str, Any], layer_name: str | None, feature_index: int | None) -> str:
    for field in GIS_NAME_FIELDS:
        value = properties.get(field)
        if value not in (None, ""):
            return str(value).strip()
    if layer_name:
        return f"{layer_name}_{feature_index or 1}"
    return f"GIS要素_{feature_index or 1}"
