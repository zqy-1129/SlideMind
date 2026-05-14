import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from bson import ObjectId

from app.core.config import settings
from app.db.mongo import get_db

ProgressLogger = Callable[[str, int], Awaitable[None]]

MAX_TEXT_CHUNKS = 300
MAX_TUPLES_PER_CHUNK = 5
TEXT_KEYWORDS = (
    "滑坡",
    "变形",
    "位移",
    "裂缝",
    "库水位",
    "降雨",
    "诱发",
    "影响",
    "风险",
    "治理",
    "防治",
    "监测",
    "稳定性",
)
RELATION_KEYWORDS = (
    "诱发",
    "导致",
    "引起",
    "影响",
    "控制",
    "治理",
    "防治",
    "加剧",
    "减缓",
    "属于",
    "位于",
    "存在",
)


@dataclass
class RegionCandidate:
    region_id: str
    region_name: str
    region_type: str
    aliases: set[str] = field(default_factory=set)
    parent_region_id: str | None = None
    source: str = "dataset"


async def enrich_kg_with_text_knowledge(
    dataset_id: str,
    kg: dict[str, Any],
    log: ProgressLogger | None = None,
) -> dict[str, Any]:
    database = get_db()
    chunks = [
        chunk
        async for chunk in database.document_chunks.find({"dataset_id": dataset_id}).sort(
            [("source_file_id", 1), ("chunk_index", 1)]
        )
    ]
    if not chunks:
        summary = _summary(enabled=True)
        kg.setdefault("meta", {})["text_kg"] = summary
        await _log(log, "未发现文本资料，跳过文本知识融合", 86)
        return summary

    await database.text_kg_tuples.delete_many({"dataset_id": dataset_id})
    regions = _build_region_dictionary(dataset_id, kg)
    alias_to_entity = _build_entity_alias_index(dataset_id, kg)

    await _log(log, f"开始文本区域锚定：chunk {len(chunks)} 个，候选区域 {len(regions)} 个", 85)

    tuple_docs: list[dict[str, Any]] = []
    tuple_ids_by_chunk: dict[str, list[str]] = {}
    created_entities: dict[str, str] = {}
    created_collections: dict[str, str] = {}
    matched_regions = 0
    unmatched_regions = 0
    processed_chunks = 0
    previous_region: RegionCandidate | None = None

    for chunk in chunks[:MAX_TEXT_CHUNKS]:
        chunk_id = str(chunk["_id"])
        text = str(chunk.get("text") or "")
        region, method, confidence = _match_region(text, regions, previous_region)
        if region and method in {"explicit", "dataset_default"}:
            previous_region = region
        if region and region.source != "dataset":
            matched_regions += 1
        else:
            unmatched_regions += 1

        tuples = await _extract_tuples(text, regions, region)
        tuples = tuples[:MAX_TUPLES_PER_CHUNK]
        tuple_ids: list[str] = []
        status = "skipped"
        if tuples:
            processed_chunks += 1
            status = "extracted"
            for item in tuples:
                anchored_region = _validate_tuple_region(item, regions) or region or _dataset_region(dataset_id, kg)
                item["region_id"] = anchored_region.region_id
                item["region_name"] = anchored_region.region_name
                item["region_match_method"] = method if anchored_region == region else "explicit"
                item["region_confidence"] = confidence if anchored_region == region else float(item.get("confidence") or 0.7)
                anchored_collection_id = _ensure_text_collection(kg, dataset_id, anchored_region, created_collections)

                subject_id = _resolve_or_create_text_entity(
                    kg,
                    dataset_id,
                    item["subject"],
                    anchored_region,
                    alias_to_entity,
                    created_entities,
                    item,
                )
                object_id = _resolve_or_create_text_entity(
                    kg,
                    dataset_id,
                    item["object"],
                    anchored_region,
                    alias_to_entity,
                    created_entities,
                    item,
                )
                _attach_text_fact(kg, subject_id, item, "subject", anchored_region, chunk_id)
                _attach_text_fact(kg, object_id, item, "object", anchored_region, chunk_id)
                _append_relation(
                    kg,
                    anchored_collection_id,
                    subject_id,
                    "包含",
                    "文本知识集合-实体关系",
                    source_id=chunk_id,
                )
                _append_relation(
                    kg,
                    anchored_collection_id,
                    object_id,
                    "包含",
                    "文本知识集合-实体关系",
                    source_id=chunk_id,
                )
                _append_relation(
                    kg,
                    subject_id,
                    object_id,
                    item["relation"],
                    "文本五元组关系",
                    relation_name=item["relation"],
                    subject=item["subject"],
                    object=item["object"],
                    time=item.get("time"),
                    location=item.get("location"),
                    region_id=anchored_region.region_id,
                    region_name=anchored_region.region_name,
                    confidence=item.get("confidence"),
                    evidence_text=item.get("evidence_text"),
                    chunk_id=chunk_id,
                    document_id=chunk.get("document_id"),
                )
                tuple_id = _hash_id("tuple", dataset_id, chunk_id, item["subject"], item["relation"], item["object"])
                tuple_ids.append(tuple_id)
                tuple_docs.append(
                    {
                        "_id": ObjectId(tuple_id[-24:]) if re.fullmatch(r"[0-9a-f]{24}", tuple_id[-24:]) else ObjectId(),
                        "tuple_id": tuple_id,
                        "dataset_id": dataset_id,
                        "document_id": chunk.get("document_id"),
                        "chunk_id": chunk_id,
                        "source_file_id": chunk.get("source_file_id"),
                        "chunk_index": chunk.get("chunk_index"),
                        "subject": item["subject"],
                        "relation": item["relation"],
                        "object": item["object"],
                        "time": item.get("time"),
                        "location": item.get("location"),
                        "region_id": anchored_region.region_id,
                        "region_name": anchored_region.region_name,
                        "region_match_method": item["region_match_method"],
                        "region_confidence": item["region_confidence"],
                        "confidence": float(item.get("confidence") or item["region_confidence"]),
                        "evidence_text": item.get("evidence_text"),
                        "status": "extracted",
                        "created_at": datetime.utcnow(),
                    }
                )

        tuple_ids_by_chunk[chunk_id] = tuple_ids
        await database.document_chunks.update_one(
            {"_id": chunk["_id"]},
            {
                "$set": {
                    "region_id": region.region_id if region else None,
                    "region_name": region.region_name if region else None,
                    "region_match_method": method,
                    "region_confidence": confidence,
                    "tuple_ids": tuple_ids,
                    "extraction_status": status,
                },
                "$unset": {"extraction_error": ""},
            },
        )

    if tuple_docs:
        await database.text_kg_tuples.insert_many(tuple_docs)

    summary = _summary(
        enabled=True,
        chunks_total=len(chunks),
        chunks_processed=processed_chunks,
        tuple_count=len(tuple_docs),
        matched_regions=matched_regions,
        unmatched_regions=unmatched_regions,
    )
    kg.setdefault("meta", {})["text_kg"] = summary
    await _log(
        log,
        f"文本知识融合完成：处理 {processed_chunks} 个 chunk，抽取 {len(tuple_docs)} 个五元组，区域匹配 {matched_regions} 个",
        90,
    )
    return summary


async def _extract_tuples(
    text: str,
    regions: list[RegionCandidate],
    matched_region: RegionCandidate | None,
) -> list[dict[str, Any]]:
    if not _is_candidate_text(text):
        return []
    llm_result = _extract_with_llm(text, regions, matched_region)
    if llm_result:
        return llm_result
    return _extract_with_rules(text, matched_region)


def _extract_with_llm(
    text: str,
    regions: list[RegionCandidate],
    matched_region: RegionCandidate | None,
) -> list[dict[str, Any]]:
    if settings.llm_provider == "mock" or not settings.openai_api_key:
        return []
    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    region_payload = [
        {
            "region_id": region.region_id,
            "region_name": region.region_name,
            "region_type": region.region_type,
            "aliases": sorted(region.aliases)[:8],
        }
        for region in regions[:40]
    ]
    prompt = (
        "你是滑坡知识图谱抽取器。请从文本中抽取实体关系型五元组，返回严格 JSON 数组。"
        "字段必须为 subject, relation, object, time, location, region_id, region_name, confidence, evidence_text。"
        "region_id 只能从候选区域中选择，无法判断时为 null。不要编造区域。\n"
        f"候选区域：{json.dumps(region_payload, ensure_ascii=False)}\n"
        f"默认区域：{matched_region.region_name if matched_region else '未知'}\n"
        f"文本：{text[:2600]}"
    )
    body = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": "只输出 JSON，不要输出 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return _parse_tuple_json(content, {region.region_id for region in regions})
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
        return []


def _extract_with_rules(text: str, matched_region: RegionCandidate | None) -> list[dict[str, Any]]:
    sentences = _split_sentences(text)
    tuples: list[dict[str, Any]] = []
    for sentence in sentences:
        if not _is_candidate_text(sentence):
            continue
        for keyword in RELATION_KEYWORDS:
            if keyword not in sentence:
                continue
            left, right = sentence.split(keyword, 1)
            subject = _clean_phrase(left[-32:]) or (matched_region.region_name if matched_region else "文本事实")
            obj = _clean_phrase(right[:40]) or "滑坡相关对象"
            tuples.append(
                {
                    "subject": subject,
                    "relation": keyword,
                    "object": obj,
                    "time": _extract_time(sentence),
                    "location": matched_region.region_name if matched_region else None,
                    "region_id": matched_region.region_id if matched_region else None,
                    "region_name": matched_region.region_name if matched_region else None,
                    "confidence": 0.58,
                    "evidence_text": sentence[:500],
                }
            )
            break
        if len(tuples) >= MAX_TUPLES_PER_CHUNK:
            break
    return tuples


def _build_region_dictionary(dataset_id: str, kg: dict[str, Any]) -> list[RegionCandidate]:
    dataset_name = str(kg.get("meta", {}).get("dataset_name") or "数据集")
    regions = [
        RegionCandidate(
            region_id=f"dataset:{dataset_id}",
            region_name=dataset_name,
            region_type="数据集",
            aliases={dataset_name},
            source="dataset",
        )
    ]
    for entity in kg.get("entities", []):
        source_kind = entity.get("source_kind")
        entity_type = str(entity.get("type") or "")
        if source_kind not in {"gis", "insar"}:
            continue
        name = str(entity.get("entity_name") or "")
        if not name:
            continue
        source = "gis_area" if entity.get("gis_category") == "area" else "gis_feature"
        aliases = _entity_aliases(entity)
        regions.append(
            RegionCandidate(
                region_id=entity["__id__"],
                region_name=name,
                region_type=entity_type,
                aliases=aliases,
                parent_region_id=entity.get("admin_entity_id"),
                source=source,
            )
        )
    return regions


def _build_entity_alias_index(dataset_id: str, kg: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    dataset_name = str(kg.get("meta", {}).get("dataset_name") or "").strip()
    for alias in {dataset_name, f"{dataset_name}滑坡" if dataset_name else ""}:
        normalized = _normalize(alias)
        if normalized:
            index.setdefault(normalized, f"dataset:{dataset_id}")
    for entity in kg.get("entities", []):
        for alias in _entity_aliases(entity):
            normalized = _normalize(alias)
            if normalized and len(normalized) >= 2:
                index.setdefault(normalized, entity["__id__"])
    return index


def _match_region(
    text: str,
    regions: list[RegionCandidate],
    previous_region: RegionCandidate | None,
) -> tuple[RegionCandidate | None, str, float]:
    normalized_text = _normalize(text)
    best: tuple[RegionCandidate | None, str, float, int] = (None, "unknown", 0.0, 0)
    for region in regions:
        for alias in region.aliases:
            normalized_alias = _normalize(alias)
            if len(normalized_alias) < 2:
                continue
            if normalized_alias in normalized_text and len(normalized_alias) > best[3]:
                method = "dataset_default" if region.source == "dataset" else "explicit"
                confidence = 0.62 if region.source == "dataset" else 0.92
                best = (region, method, confidence, len(normalized_alias))
    if best[0] is not None:
        return best[0], best[1], best[2]
    if previous_region is not None:
        return previous_region, "inherited", 0.68
    return None, "unknown", 0.0


def _validate_tuple_region(item: dict[str, Any], regions: list[RegionCandidate]) -> RegionCandidate | None:
    region_id = item.get("region_id")
    if not region_id:
        return None
    return next((region for region in regions if region.region_id == region_id), None)


def _resolve_or_create_text_entity(
    kg: dict[str, Any],
    dataset_id: str,
    name: str,
    region: RegionCandidate,
    alias_to_entity: dict[str, str],
    created_entities: dict[str, str],
    tuple_item: dict[str, Any],
) -> str:
    normalized = _normalize(name)
    if normalized in alias_to_entity:
        return alias_to_entity[normalized]
    for alias, entity_id in alias_to_entity.items():
        if len(alias) >= 3 and (alias in normalized or normalized in alias):
            return entity_id
    key = f"{region.region_id}:{normalized}"
    if key in created_entities:
        return created_entities[key]
    entity_id = _hash_id("text-entity", dataset_id, region.region_id, name)
    kg["entities"].append(
        {
            "__id__": entity_id,
            "__created_at__": _timestamp(),
            "entity_name": name,
            "type": _classify_text_entity(name, tuple_item),
            "geom_type": "Text",
            "geometry": None,
            "attributes": {
                "region_id": region.region_id,
                "region_name": region.region_name,
                "source": "text_kg",
            },
            "dataset_id": dataset_id,
            "source_kind": "text_entity",
            "admin_belong": region.region_name,
        }
    )
    created_entities[key] = entity_id
    alias_to_entity[normalized] = entity_id
    return entity_id


def _attach_text_fact(
    kg: dict[str, Any],
    entity_id: str,
    item: dict[str, Any],
    role: str,
    region: RegionCandidate,
    chunk_id: str,
) -> None:
    fact = {
        "role": role,
        "subject": item.get("subject"),
        "relation": item.get("relation"),
        "object": item.get("object"),
        "time": item.get("time"),
        "location": item.get("location"),
        "region_id": region.region_id,
        "region_name": region.region_name,
        "confidence": item.get("confidence"),
        "evidence_text": item.get("evidence_text"),
        "chunk_id": chunk_id,
    }
    if entity_id.startswith("dataset:"):
        facts = kg.setdefault("meta", {}).setdefault("text_facts", [])
        _append_unique_fact(facts, fact)
        kg["meta"]["text_fact_count"] = len(facts)
        return

    entity = next((entry for entry in kg.get("entities", []) if entry.get("__id__") == entity_id), None)
    if entity is None:
        return
    attributes = entity.setdefault("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}
        entity["attributes"] = attributes
    facts = attributes.setdefault("text_facts", [])
    if not isinstance(facts, list):
        facts = []
        attributes["text_facts"] = facts
    _append_unique_fact(facts, fact)
    attributes["text_fact_count"] = len(facts)


def _append_unique_fact(facts: list[dict[str, Any]], fact: dict[str, Any], limit: int = 30) -> None:
    key = (fact.get("subject"), fact.get("relation"), fact.get("object"), fact.get("chunk_id"))
    for existing in facts:
        if (existing.get("subject"), existing.get("relation"), existing.get("object"), existing.get("chunk_id")) == key:
            return
    facts.append(fact)
    del facts[limit:]


def _ensure_text_collection(
    kg: dict[str, Any],
    dataset_id: str,
    region: RegionCandidate | None,
    created_collections: dict[str, str],
) -> str:
    region = region or _dataset_region(dataset_id, kg)
    key = region.region_id
    if key in created_collections:
        return created_collections[key]
    collection_id = _hash_id("text-collection", dataset_id, region.region_id)
    name = f"{region.region_name}_文本知识集合"
    kg["entities"].append(
        {
            "__id__": collection_id,
            "__created_at__": _timestamp(),
            "entity_name": name,
            "type": "文本知识集合",
            "geom_type": "Collection",
            "geometry": None,
            "attributes": {
                "region_id": region.region_id,
                "region_name": region.region_name,
                "region_type": region.region_type,
                "source": region.source,
            },
            "dataset_id": dataset_id,
            "source_kind": "text_collection",
        }
    )
    _append_relation(
        kg,
        region.region_id,
        collection_id,
        "文本知识",
        "区域-文本知识集合关系",
        region_id=region.region_id,
        region_name=region.region_name,
    )
    created_collections[key] = collection_id
    return collection_id


def _ensure_document_entity(kg: dict[str, Any], document: dict[str, Any] | None, created_documents: set[str]) -> str:
    document_id = str(document["_id"]) if document else "unknown"
    entity_id = _hash_id("document", document_id)
    if entity_id in created_documents:
        return entity_id
    kg["entities"].append(
        {
            "__id__": entity_id,
            "__created_at__": _timestamp(),
            "entity_name": document.get("title") if document else "未知文档",
            "type": "Document",
            "geom_type": "Text",
            "geometry": None,
            "attributes": {
                "document_id": document_id,
                "source_file_id": document.get("source_file_id") if document else None,
            },
            "dataset_id": document.get("dataset_id") if document else None,
            "mongo_id": document_id,
            "source_file_id": document.get("source_file_id") if document else None,
            "source_kind": "document",
        }
    )
    created_documents.add(entity_id)
    return entity_id


def _ensure_chunk_entity(
    kg: dict[str, Any],
    chunk: dict[str, Any],
    document_entity_id: str,
    created_chunks: set[str],
) -> str:
    chunk_id = str(chunk["_id"])
    entity_id = _hash_id("chunk", chunk_id)
    if entity_id in created_chunks:
        return entity_id
    kg["entities"].append(
        {
            "__id__": entity_id,
            "__created_at__": _timestamp(),
            "entity_name": f"文本片段_{chunk.get('chunk_index', 0)}",
            "type": "DocumentChunk",
            "geom_type": "Text",
            "geometry": None,
            "attributes": {
                "chunk_id": chunk_id,
                "chunk_index": chunk.get("chunk_index"),
                "text": str(chunk.get("text") or "")[:1200],
                "milvus_vector_id": chunk.get("milvus_vector_id"),
            },
            "dataset_id": chunk.get("dataset_id"),
            "mongo_id": chunk_id,
            "source_file_id": chunk.get("source_file_id"),
            "source_kind": "document_chunk",
        }
    )
    _append_relation(kg, document_entity_id, entity_id, "包含", "文档-切片关系", source_id=chunk_id)
    created_chunks.add(entity_id)
    return entity_id


async def _load_documents(dataset_id: str) -> dict[str, dict[str, Any]]:
    documents = get_db().documents.find({"dataset_id": dataset_id})
    return {str(document["_id"]): document async for document in documents}


def _parse_tuple_json(content: str, valid_region_ids: set[str]) -> list[dict[str, Any]]:
    clean = content.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?", "", clean).strip()
        clean = re.sub(r"```$", "", clean).strip()
    data = json.loads(clean)
    if isinstance(data, dict):
        data = data.get("tuples") or data.get("items") or []
    if not isinstance(data, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in data[:MAX_TUPLES_PER_CHUNK]:
        if not isinstance(raw, dict):
            continue
        subject = _clean_phrase(raw.get("subject"))
        relation = _clean_phrase(raw.get("relation"))
        obj = _clean_phrase(raw.get("object"))
        if not subject or not relation or not obj:
            continue
        region_id = raw.get("region_id") if raw.get("region_id") in valid_region_ids else None
        items.append(
            {
                "subject": subject,
                "relation": relation,
                "object": obj,
                "time": _clean_phrase(raw.get("time")),
                "location": _clean_phrase(raw.get("location")),
                "region_id": region_id,
                "region_name": _clean_phrase(raw.get("region_name")) if region_id else None,
                "confidence": _safe_float(raw.get("confidence"), 0.72),
                "evidence_text": _clean_phrase(raw.get("evidence_text")),
            }
        )
    return items


def _entity_aliases(entity: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for value in (entity.get("entity_name"), entity.get("admin_belong"), entity.get("source_file")):
        aliases.update(_split_aliases(value))
    attributes = entity.get("attributes") or {}
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            key_text = str(key).lower()
            if "name" in key_text or key in {"名称", "地名", "admin_name"}:
                aliases.update(_split_aliases(value))
    return {alias for alias in aliases if not _is_weak_alias(alias)}


def _split_aliases(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    text = str(value).strip()
    if not text:
        return set()
    return {part.strip() for part in re.split(r"[,，;；/、\s]+", text) if part.strip()}


def _dataset_region(dataset_id: str, kg: dict[str, Any]) -> RegionCandidate:
    name = str(kg.get("meta", {}).get("dataset_name") or "数据集")
    return RegionCandidate(region_id=f"dataset:{dataset_id}", region_name=name, region_type="数据集", aliases={name})


def _append_relation(kg: dict[str, Any], src_id: str, tgt_id: str, content: str, relation_type: str, **extra: Any) -> None:
    relation = {
        "__id__": _hash_id("rel", src_id, tgt_id, content, relation_type, extra.get("chunk_id") or extra.get("source_id")),
        "__created_at__": _timestamp(),
        "src_id": src_id,
        "tgt_id": tgt_id,
        "content": content,
        "type": relation_type,
        **extra,
    }
    key = (relation["src_id"], relation["tgt_id"], relation["type"], relation["content"], relation.get("chunk_id"))
    existing = {
        (item.get("src_id"), item.get("tgt_id"), item.get("type"), item.get("content"), item.get("chunk_id"))
        for item in kg.get("relations", [])
    }
    if key not in existing:
        kg.setdefault("relations", []).append(relation)


def _classify_text_entity(name: str, item: dict[str, Any]) -> str:
    text = f"{name}{item.get('relation', '')}{item.get('object', '')}"
    if any(word in text for word in ("风险", "危险", "隐患", "失稳")):
        return "RiskFactor"
    if any(word in text for word in ("治理", "防治", "支护", "排水", "加固", "监测")):
        return "EngineeringMeasure"
    if any(word in text for word in ("变形", "裂缝", "滑移", "异常", "事件")):
        return "TextEvent"
    return "TextEntity"


def _summary(
    enabled: bool,
    chunks_total: int = 0,
    chunks_processed: int = 0,
    tuple_count: int = 0,
    matched_regions: int = 0,
    unmatched_regions: int = 0,
) -> dict[str, Any]:
    return {
        "text_kg_enabled": enabled,
        "text_chunks_total": chunks_total,
        "text_chunks_processed": chunks_processed,
        "text_tuple_count": tuple_count,
        "text_region_matched": matched_regions,
        "text_region_unmatched": unmatched_regions,
    }


def _is_candidate_text(text: str) -> bool:
    return any(keyword in text for keyword in TEXT_KEYWORDS)


def _split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?；;\n]+", text) if item.strip()]


def _extract_time(text: str) -> str | None:
    match = re.search(r"(\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?|\d{4}-\d{1,2}(?:-\d{1,2})?)", text)
    return match.group(1) if match else None


def _clean_phrase(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", "", str(value).strip(" ：:，,。；;、"))
    return text[:80] if text else None


def _normalize(value: Any) -> str:
    return re.sub(r"[\s,，;；/、._\-:：()（）]+", "", str(value or "").lower())


def _is_weak_alias(alias: str) -> bool:
    normalized = _normalize(alias)
    return not normalized or normalized.isdigit() or bool(re.fullmatch(r"[0-9a-f]{8,}", normalized))


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp() -> float:
    return datetime.utcnow().timestamp()


def _hash_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1(":".join(str(part) for part in parts if part is not None).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


async def _log(log: ProgressLogger | None, message: str, progress: int) -> None:
    if log is not None:
        await log(message, progress)
