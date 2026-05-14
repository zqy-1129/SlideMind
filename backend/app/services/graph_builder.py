from typing import Any

from neo4j import Driver

from app.db.neo4j import get_driver
from app.services.spatial_kg import ProgressLogger, build_spatial_kg_from_mongo, write_kg_to_neo4j
from app.services.text_kg import enrich_kg_with_text_knowledge


async def build_graph_for_dataset(
    dataset_id: str,
    log: ProgressLogger | None = None,
    include_text_kg: bool = True,
) -> dict[str, int]:
    kg = await build_spatial_kg_from_mongo(dataset_id, log=log)
    if include_text_kg:
        if log is not None:
            await log("融合文本五元组知识", 84)
        text_summary = await enrich_kg_with_text_knowledge(dataset_id, kg, log=log)
    else:
        text_summary = {"text_kg_enabled": False}
        kg.setdefault("meta", {})["text_kg"] = text_summary
        if log is not None:
            await log("已关闭文本知识融合", 84)
    if log is not None:
        await log("写入 Neo4j", 92)
    summary = write_kg_to_neo4j(get_driver(), dataset_id, kg)
    summary.update(text_summary)
    if log is not None:
        await log("Neo4j 写入完成", 100)
    return summary


def read_graph(
    dataset_id: str | None = None,
    limit: int = 20,
    node_type: str | None = None,
    parent_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    driver = get_driver()
    safe_limit = min(max(limit, 1), 1000)
    if parent_id:
        return _read_graph_children(driver, dataset_id, parent_id, safe_limit)
    if dataset_id and not node_type:
        return _read_graph_roots(driver, dataset_id, safe_limit)

    node_query = """
    MATCH (n)
    WHERE ($dataset_id IS NULL OR n.dataset_id = $dataset_id)
      AND ($node_type IS NULL OR n.entity_type = $node_type)
    RETURN n
    ORDER BY CASE WHEN n:DatasetGraph THEN 0 ELSE 1 END, coalesce(n.entity_type, ''), coalesce(n.name, '')
    LIMIT $limit
    """

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    with driver.session() as session:
        result = session.run(node_query, dataset_id=dataset_id, node_type=node_type or None, limit=safe_limit)
        for row in result:
            node = row["n"]
            node_id = node.get("id") or str(node.element_id)
            nodes[node_id] = _graph_node(node, node_id)

        if nodes:
            edge_query = """
            MATCH (n)-[r]->(m)
            WHERE n.id IN $node_ids AND m.id IN $node_ids
            RETURN n, r, m
            LIMIT $edge_limit
            """
            result = session.run(edge_query, node_ids=list(nodes.keys()), edge_limit=safe_limit * 4)
            for row in result:
                for key in ("n", "m"):
                    node = row[key]
                    node_id = node.get("id") or str(node.element_id)
                    nodes[node_id] = _graph_node(node, node_id)
                rel = row["r"]
                edge_id = rel.get("id") or str(rel.element_id)
                edges[edge_id] = {
                    "id": edge_id,
                    "source": row["n"].get("id") or str(row["n"].element_id),
                    "target": row["m"].get("id") or str(row["m"].element_id),
                    "label": rel.get("content") or rel.type,
                    "properties": dict(rel),
                }

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _read_graph_roots(driver: Driver, dataset_id: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    query = """
    MATCH (d:DatasetGraph {dataset_id: $dataset_id})
    OPTIONAL MATCH (d)-[r:HAS_ENTITY|HAS_TEXT_KNOWLEDGE]->(child)
    WHERE child:Area OR (child.source_kind = 'text_collection' AND child.region_id = d.id)
    RETURN d, r, child
    ORDER BY CASE WHEN child:Area THEN 0 ELSE 9 END, coalesce(child.name, '')
    LIMIT $limit
    """
    fallback_query = """
    MATCH (d:DatasetGraph {dataset_id: $dataset_id})
    OPTIONAL MATCH (d)-[r:HAS_ENTITY]->(child)
    WHERE child.dataset_id = $dataset_id
    RETURN d, r, child
    ORDER BY coalesce(child.scale_level, 99), coalesce(child.entity_type, ''), coalesce(child.name, '')
    LIMIT $limit
    """
    with driver.session() as session:
        result = list(session.run(query, dataset_id=dataset_id, limit=limit))
        child_count = sum(1 for row in result if row["child"] is not None)
        if child_count == 0:
            result = list(session.run(fallback_query, dataset_id=dataset_id, limit=limit))
        for row in result:
            _add_node(nodes, row["d"])
            if row["child"] is not None and row["r"] is not None:
                _add_node(nodes, row["child"])
                _add_edge(edges, row["d"], row["r"], row["child"])
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _read_graph_children(driver: Driver, dataset_id: str | None, parent_id: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    dataset_filter = "AND child.dataset_id = $dataset_id" if dataset_id else ""
    parent_filter = "AND parent.dataset_id = $dataset_id" if dataset_id else ""
    dataset_child_filter = "AND other.dataset_id = $dataset_id" if dataset_id else ""
    root_query = f"""
    MATCH (parent:DatasetGraph {{id: $parent_id}})
    OPTIONAL MATCH (parent)-[r:HAS_ENTITY]->(child:Area)
    WHERE child IS NULL OR true {dataset_filter}
    RETURN parent, r, child
    ORDER BY coalesce(child.name, '')
    LIMIT $limit
    """
    contains_query = f"""
    MATCH (parent {{id: $parent_id}})
    WHERE true {parent_filter}
    OPTIONAL MATCH (parent)-[r:CONTAINS|HAS_TEXT_KNOWLEDGE]->(child)
    WHERE child IS NULL OR true {dataset_filter}
    RETURN parent, r, child
    ORDER BY
      CASE
        WHEN child.entity_type CONTAINS '交通' THEN 0
        WHEN child.entity_type CONTAINS '水' THEN 1
        WHEN child.entity_type CONTAINS '建筑' THEN 2
        WHEN child.entity_type CONTAINS '文本' THEN 3
        ELSE 9
      END,
      coalesce(child.entity_type, ''),
      coalesce(child.name, '')
    LIMIT $limit
    """
    related_query = f"""
    MATCH (parent {{id: $parent_id}})
    WHERE true {parent_filter}
    OPTIONAL MATCH (parent)-[r]-(other)
    WHERE other IS NULL OR (type(r) <> 'HAS_ENTITY' {dataset_child_filter})
    RETURN parent, r, other AS child
    ORDER BY type(r), coalesce(other.entity_type, ''), coalesce(other.name, '')
    LIMIT $limit
    """
    with driver.session() as session:
        result = list(session.run(root_query, dataset_id=dataset_id, parent_id=parent_id, limit=limit))
        if not result:
            result = list(session.run(contains_query, dataset_id=dataset_id, parent_id=parent_id, limit=limit))
        if _result_child_count(result) == 0:
            result = list(session.run(contains_query, dataset_id=dataset_id, parent_id=parent_id, limit=limit))
        if _result_child_count(result) == 0:
            related_rows = list(session.run(related_query, dataset_id=dataset_id, parent_id=parent_id, limit=limit))
            for row in related_rows:
                _add_node(nodes, row["parent"])
                if row["child"] is not None and row["r"] is not None:
                    _add_node(nodes, row["child"])
                    _add_edge(edges, row["parent"], row["r"], row["child"])
            return {"nodes": list(nodes.values()), "edges": list(edges.values())}

        for row in result:
            _add_node(nodes, row["parent"])
            if row["child"] is not None and row["r"] is not None:
                _add_node(nodes, row["child"])
                _add_edge(edges, row["parent"], row["r"], row["child"])
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _result_child_count(rows: list[Any]) -> int:
    return sum(1 for row in rows if row["child"] is not None)


def read_graph_node_types(dataset_id: str | None = None) -> list[str]:
    driver = get_driver()
    query = """
    MATCH (n)
    WHERE ($dataset_id IS NULL OR n.dataset_id = $dataset_id)
      AND n.entity_type IS NOT NULL
    RETURN DISTINCT n.entity_type AS type
    ORDER BY type
    """
    with driver.session() as session:
        return [row["type"] for row in session.run(query, dataset_id=dataset_id)]


def _graph_node(node: Any, node_id: str) -> dict[str, Any]:
    labels = list(node.labels)
    return {
        "id": node_id,
        "label": node.get("name") or node_id,
        "type": node.get("entity_type") or (labels[0] if labels else "Entity"),
        "properties": dict(node),
    }


def _add_node(nodes: dict[str, dict[str, Any]], node: Any) -> str:
    node_id = node.get("id") or str(node.element_id)
    nodes[node_id] = _graph_node(node, node_id)
    return node_id


def _add_edge(edges: dict[str, dict[str, Any]], source: Any, rel: Any, target: Any) -> None:
    edge_id = rel.get("id") or str(rel.element_id)
    edges[edge_id] = {
        "id": edge_id,
        "source": source.get("id") or str(source.element_id),
        "target": target.get("id") or str(target.element_id),
        "label": rel.get("content") or rel.type,
        "properties": dict(rel),
    }


def _legacy_read_connected_graph(driver: Driver, dataset_id: str | None, limit: int) -> dict[str, list[dict[str, Any]]]:
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
                nodes[node_id] = _graph_node(node, node_id)
            rel = row["r"]
            edge_id = rel.get("id") or str(rel.element_id)
            edges[edge_id] = {
                "id": edge_id,
                "source": row["n"].get("id") or str(row["n"].element_id),
                "target": row["m"].get("id") or str(row["m"].element_id),
                "label": rel.get("content") or rel.type,
                "properties": dict(rel),
            }
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
