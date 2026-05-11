from datetime import datetime
from typing import Any

from app.db.milvus import get_collection
from app.db.mongo import get_db
from app.db.neo4j import get_driver
from app.services.embedding import embed_text


async def answer_question(question: str, dataset_id: str | None) -> dict[str, Any]:
    route = _classify_question(question)
    if route == "mongodb":
        result = await _answer_from_mongo(question, dataset_id)
    elif route == "neo4j":
        result = _answer_from_neo4j(question, dataset_id)
    elif route == "milvus":
        result = _answer_from_milvus(question, dataset_id)
    else:
        mongo_result = await _answer_from_mongo(question, dataset_id)
        graph_result = _answer_from_neo4j(question, dataset_id)
        vector_result = _answer_from_milvus(question, dataset_id)
        sources = mongo_result["sources"] + graph_result["sources"] + vector_result["sources"]
        result = {
            "answer": "已综合监测记录、图谱关系和文本资料生成初步回答。"
            + _summarize_sources(sources),
            "route": "hybrid",
            "sources": sources,
        }

    await get_db().qa_records.insert_one(
        {
            "question": question,
            "answer": result["answer"],
            "route": result["route"],
            "sources": result["sources"],
            "dataset_id": dataset_id,
            "created_at": datetime.utcnow(),
        }
    )
    return result


def _classify_question(question: str) -> str:
    q = question.lower()
    if any(word in q for word in ["形变", "位移", "水位", "趋势", "最近", "监测", "insar"]):
        if any(word in q for word in ["关系", "关联", "影响", "导致"]):
            return "hybrid"
        return "mongodb"
    if any(word in q for word in ["属于", "哪些", "图谱", "关系", "关联", "节点"]):
        return "neo4j"
    if any(word in q for word in ["报告", "资料", "文本", "诱因", "原因", "描述"]):
        return "milvus"
    return "hybrid"


async def _answer_from_mongo(question: str, dataset_id: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if dataset_id:
        query["dataset_id"] = dataset_id
    cursor = get_db().tabular_records.find(query).sort("timestamp", -1).limit(8)
    records = [record async for record in cursor]
    if not records:
        return {"answer": "MongoDB 中暂未找到可用于回答的监测记录。", "route": "mongodb", "sources": []}

    values = [record.get("normalized_fields", {}) for record in records]
    answer = f"已查询到 {len(values)} 条最近监测记录。"
    if any("displacement" in value for value in values):
        displacements = [value["displacement"] for value in values if isinstance(value.get("displacement"), (int, float))]
        if displacements:
            answer += f" 位移范围约为 {min(displacements):.2f} 至 {max(displacements):.2f}。"
    if any("water_level" in value for value in values):
        levels = [value["water_level"] for value in values if isinstance(value.get("water_level"), (int, float))]
        if levels:
            answer += f" 库水位范围约为 {min(levels):.2f} 至 {max(levels):.2f}。"

    return {
        "answer": answer,
        "route": "mongodb",
        "sources": [
            {
                "type": "tabular_record",
                "id": str(record["_id"]),
                "normalized_fields": record.get("normalized_fields", {}),
            }
            for record in records
        ],
    }


def _answer_from_neo4j(question: str, dataset_id: str | None) -> dict[str, Any]:
    query = """
    MATCH (n)-[r]->(m)
    WHERE $dataset_id IS NULL OR n.dataset_id = $dataset_id OR m.dataset_id = $dataset_id
    RETURN n, type(r) AS rel, m
    LIMIT 12
    """
    sources = []
    with get_driver().session() as session:
        result = session.run(query, dataset_id=dataset_id)
        for row in result:
            sources.append(
                {
                    "type": "graph_relation",
                    "source": row["n"].get("name") or row["n"].get("id"),
                    "relation": row["rel"],
                    "target": row["m"].get("name") or row["m"].get("id"),
                }
            )
    if not sources:
        return {"answer": "Neo4j 图谱中暂未找到相关关系。", "route": "neo4j", "sources": []}
    return {
        "answer": "图谱中找到相关实体关系：" + "；".join(
            f"{item['source']} -{item['relation']}-> {item['target']}" for item in sources[:5]
        ),
        "route": "neo4j",
        "sources": sources,
    }


def _answer_from_milvus(question: str, dataset_id: str | None) -> dict[str, Any]:
    collection = get_collection()
    expr = f'dataset_id == "{dataset_id}"' if dataset_id else ""
    search_kwargs: dict[str, Any] = {
        "data": [embed_text(question)],
        "anns_field": "embedding",
        "param": {"metric_type": "COSINE", "params": {}},
        "limit": 5,
        "output_fields": ["mongo_id", "dataset_id", "source_type", "text"],
    }
    if expr:
        search_kwargs["expr"] = expr
    result = collection.search(**search_kwargs)
    hits = result[0] if result else []
    sources = [
        {
            "type": hit.entity.get("source_type"),
            "mongo_id": hit.entity.get("mongo_id"),
            "dataset_id": hit.entity.get("dataset_id"),
            "score": hit.score,
            "text": hit.entity.get("text"),
        }
        for hit in hits
    ]
    if not sources:
        return {"answer": "Milvus 中暂未检索到相关文本片段。", "route": "milvus", "sources": []}
    return {
        "answer": "从文本资料中检索到相关内容：" + sources[0]["text"][:180],
        "route": "milvus",
        "sources": sources,
    }


def _summarize_sources(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return " 当前数据库中没有足够数据支撑更具体的回答。"
    return f" 本次共引用 {len(sources)} 条来源，请在来源列表中查看详情。"
