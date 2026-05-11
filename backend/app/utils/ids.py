from bson import ObjectId


def oid() -> str:
    return str(ObjectId())


def stringify_id(document: dict) -> dict:
    data = dict(document)
    data["id"] = str(data.pop("_id"))
    return data

