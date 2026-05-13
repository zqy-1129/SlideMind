from typing import Any

from neo4j import Driver

from app.db.neo4j import get_driver
from app.services.spatial_kg import ProgressLogger, build_spatial_kg_from_mongo, write_kg_to_neo4j


async def build_graph_for_dataset(dataset_id: str, log: ProgressLogger | None = None) -> dict[str, int]:
    kg = await build_spatial_kg_from_mongo(dataset_id, log=log)
    if log is not None:
        await log("写入 Neo4j", 88)
    summary = write_kg_to_neo4j(get_driver(), dataset_id, kg)
    if log is not None:
        await log("Neo4j 写入完成", 100)
    return summary


def read_graph(dataset_id: str | None = None, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
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
                "label": rel.get("content") or rel.type,
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
