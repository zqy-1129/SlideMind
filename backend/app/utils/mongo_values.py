from datetime import datetime
from math import isnan
from typing import Any


def clean_for_mongo(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and isnan(value):
        return None
    if isinstance(value, dict):
        return {str(key): clean_for_mongo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_for_mongo(item) for item in value]
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        try:
            return clean_for_mongo(value.item())
        except ValueError:
            return str(value)
    if isinstance(value, datetime):
        return value
    return value
