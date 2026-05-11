from typing import Any

from neo4j import Driver

from app.db.mongo import get_db
from app.db.neo4j import get_driver


async def build_graph_for_dataset(dataset_id: str) -> dict[str, int]:
    database = get_db()
    driver = get_driver()
    records = database.tabular_records.find({"dataset_id": dataset_id})

    landslides: set[str] = set()
    monitor_points: dict[str, dict[str, Any]] = {}
    reservoir_stations: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    async for record in records:
        normalized = record.get("normalized_fields", {})
        landslide_name = normalized.get("landslide_name") or "未命名滑坡"
        landslides.add(landslide_name)

        point_id = normalized.get("point_id")
        if point_id:
            monitor_points[point_id] = {
                "id": f"monitor:{dataset_id}:{point_id}",
                "name": point_id,
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
                "name": station_name,
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

    with driver.session() as session:
        for landslide_name in landslides:
            session.execute_write(_merge_landslide, dataset_id, landslide_name)
        for point in monitor_points.values():
            session.execute_write(_merge_monitor_point, point)
        for station in reservoir_stations.values():
            session.execute_write(_merge_reservoir_station, station)
        for event in events:
            session.execute_write(_merge_event, event)

    return {
        "landslides": len(landslides),
        "monitor_points": len(monitor_points),
        "reservoir_stations": len(reservoir_stations),
        "events": len(events),
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
                    "type": labels[0] if labels else "Entity",
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
                    "type": labels[0] if labels else "Entity",
                    "properties": dict(node),
                }
            )
    return {"nodes": nodes, "edges": []}


def _merge_landslide(tx, dataset_id: str, name: str) -> None:
    tx.run(
        """
        MERGE (l:Landslide:Entity {id: $id})
        SET l.name = $name, l.dataset_id = $dataset_id
        """,
        id=f"landslide:{dataset_id}:{name}",
        name=name,
        dataset_id=dataset_id,
    )


def _merge_monitor_point(tx, point: dict[str, Any]) -> None:
    tx.run(
        """
        MATCH (l:Landslide {id: $landslide_id})
        MERGE (p:MonitorPoint:InSARPoint:Entity {id: $id})
        SET p += $props
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
        SET s += $props
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
        SET e += $props
        MERGE (l)-[:HAS_EVENT {id: $rel_id}]->(e)
        """,
        landslide_id=f"landslide:{event['dataset_id']}:{event['landslide_name']}",
        id=event["id"],
        props=event,
        rel_id=f"rel:event:{event['id']}",
    )
