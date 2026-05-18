import json
import re
import urllib.error
import urllib.request
from typing import Any

from app.core.config import settings

VALID_RELATION_TYPES = {
    "导致",
    "触发",
    "影响",
    "约束",
    "推理",
    "驱动",
    "适用",
    "关联",
    "组成",
    "相交",
    "演化",
    "等价",
    "对齐",
    "包含",
}

RELATION_ALIASES = {
    "引起": "导致",
    "造成": "导致",
    "诱发": "触发",
    "加剧": "影响",
    "减缓": "影响",
    "控制": "约束",
    "治理": "影响",
    "防治": "影响",
    "位于": "包含",
    "属于": "包含",
    "相关": "关联",
    "相关于": "关联",
}

ALIAS_DICT = {
    "地表形变": "地面沉降",
    "地表沉降": "地面沉降",
    "时序InSAR": "InSAR",
    "SBAS-InSAR": "SBAS",
    "SBAS InSAR": "SBAS",
    "PS InSAR": "PS-InSAR",
    "D-InSAR": "InSAR",
}

INVALID_NAME_RE = re.compile(r"^[\s\d\W_]+$")


def extract_stkg_tuples(
    text: str,
    region_payload: list[dict[str, Any]],
    matched_region_name: str | None,
    valid_region_ids: set[str],
) -> list[dict[str, Any]]:
    if settings.llm_provider == "mock" or not settings.openai_api_key:
        return []
    prompt = _build_prompt(text, region_payload, matched_region_name)
    body = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": "你是地质灾害时空知识图谱抽取器。只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    request = urllib.request.Request(
        f"{(settings.openai_base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
        return []
    return parse_extraction_output(content, valid_region_ids)


def parse_extraction_output(content: str, valid_region_ids: set[str]) -> list[dict[str, Any]]:
    data = _json_loads_loose(content)
    if data is None:
        return []
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict) and isinstance(data.get("tuples"), list):
        raw_items = data["tuples"]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        raw_items = data["items"]
    elif isinstance(data, dict) and ("entities" in data or "relations" in data):
        raw_items = _stkg_to_tuple_candidates(data)
    else:
        raw_items = []
    return _clean_tuple_items(raw_items, valid_region_ids)


def _build_prompt(text: str, region_payload: list[dict[str, Any]], matched_region_name: str | None) -> str:
    return (
        "请从给定滑坡文本中按观测层、事件层、属性层、规则层抽取知识，并转换为五元组 JSON 数组。\n"
        "五元组字段必须为：subject, relation, object, time, location, region_id, region_name, confidence, evidence_text。\n"
        "relation 必须优先使用：导致、触发、影响、约束、推理、驱动、适用、关联、组成、相交、演化、等价、对齐、包含。\n"
        "region_id 只能从候选区域中选择；无法判断时返回 null，禁止编造区域。\n"
        "抽取要求：\n"
        "1. 观测层：抽取监测对象、库水位、降雨、位移、变形等观测事实。\n"
        "2. 事件层：抽取滑坡、变形异常、裂缝、治理等事件及其关系。\n"
        "3. 属性层：把地势、岩性、风险等级、形变趋势等属性转为实体属性型五元组。\n"
        "4. 规则层：把诱因、阈值、条件-结论、因果链转为关系五元组。\n"
        "5. 不要输出 Markdown，不要输出解释文字。\n"
        f"候选区域：{json.dumps(region_payload, ensure_ascii=False)}\n"
        f"默认区域：{matched_region_name or '未知'}\n"
        f"文本：{text[:3200]}"
    )


def _json_loads_loose(content: str) -> Any:
    clean = (content or "").strip()
    clean = re.sub(r"^```(?:json)?", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()
    clean = re.sub(r"//.*?(?=\n|$)", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(clean[start : end + 1])
            except json.JSONDecodeError:
                return None
        start = clean.find("[")
        end = clean.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(clean[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _stkg_to_tuple_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    entities = {
        item.get("__id__"): item
        for item in data.get("entities", [])
        if isinstance(item, dict) and item.get("__id__")
    }
    tuples: list[dict[str, Any]] = []
    for relation in data.get("relations", []):
        if not isinstance(relation, dict):
            continue
        source = entities.get(relation.get("src_id"), {})
        target = entities.get(relation.get("tgt_id"), {})
        properties = relation.get("properties") if isinstance(relation.get("properties"), dict) else {}
        subject = source.get("entity_name") or relation.get("subject")
        obj = target.get("entity_name") or relation.get("object")
        relation_type = properties.get("relation_type") or relation.get("relation_type") or relation.get("content")
        tuples.append(
            {
                "subject": subject,
                "relation": relation_type,
                "object": obj,
                "time": _temporal_text(relation.get("temporal")),
                "location": properties.get("region") or properties.get("location"),
                "confidence": properties.get("score") or properties.get("confidence"),
                "evidence_text": relation.get("content"),
            }
        )
    for entity in data.get("entities", []):
        if not isinstance(entity, dict):
            continue
        properties = entity.get("properties") if isinstance(entity.get("properties"), dict) else {}
        for key, value in properties.items():
            if value in (None, "") or key in {"score", "confidence"}:
                continue
            tuples.append(
                {
                    "subject": entity.get("entity_name"),
                    "relation": "包含",
                    "object": f"{key}:{value}",
                    "time": _temporal_text(entity.get("temporal")),
                    "location": properties.get("region") or properties.get("location"),
                    "confidence": properties.get("score") or 0.66,
                    "evidence_text": entity.get("content"),
                }
            )
    return tuples


def _clean_tuple_items(raw_items: list[Any], valid_region_ids: set[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        subject = _clean_name(raw.get("subject"))
        relation = _normalize_relation(raw.get("relation"))
        obj = _clean_name(raw.get("object"))
        if not subject or not relation or not obj or subject == obj:
            continue
        confidence = _safe_float(raw.get("confidence"), 0.72)
        if confidence < 0.1:
            continue
        key = (_normalize_text(subject), relation, _normalize_text(obj))
        if key in seen:
            continue
        seen.add(key)
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
                "confidence": confidence,
                "evidence_text": _clean_evidence(raw.get("evidence_text")),
            }
        )
    return items


def _normalize_relation(value: Any) -> str | None:
    relation = _clean_phrase(value)
    if not relation:
        return None
    relation = RELATION_ALIASES.get(relation, relation)
    return relation if relation in VALID_RELATION_TYPES else "关联"


def _clean_name(value: Any) -> str | None:
    text = _clean_phrase(value)
    if not text or INVALID_NAME_RE.fullmatch(text):
        return None
    return ALIAS_DICT.get(text, text)


def _clean_phrase(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", "", str(value).strip(" ：:，,。；;、"))
    return text[:120] if text else None


def _clean_evidence(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:500] if text else None


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s,，;；/、._\-:：()（）]+", "", value.lower())


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _temporal_text(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        keys = []
        for item in value.values():
            if isinstance(item, dict):
                keys.extend(str(key) for key in item.keys())
        if keys:
            return "、".join(keys[:3])
    return None
