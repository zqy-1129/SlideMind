import json
from pathlib import Path
from typing import Any

import pandas as pd
from docx import Document
from pypdf import PdfReader


TABLE_EXTENSIONS = {".csv", ".xlsx", ".xls"}
TEXT_EXTENSIONS = {".txt", ".docx", ".pdf"}
GIS_EXTENSIONS = {".geojson", ".json"}


def parse_table(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if file_path.suffix.lower() == ".csv":
        frame = _read_csv_with_fallback(file_path)
    else:
        frame = pd.read_excel(file_path)
    frame = frame.where(pd.notnull(frame), None)
    return frame.to_dict(orient="records")


def _read_csv_with_fallback(file_path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin1"):
        try:
            return pd.read_csv(file_path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Unable to decode CSV file with common encodings: {last_error}") from last_error


def parse_text(path: str) -> str:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".docx":
        document = Document(str(file_path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
    if suffix == ".pdf":
        reader = PdfReader(str(file_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError(f"Unsupported text extension: {suffix}")


def parse_geojson(path: str) -> dict[str, Any]:
    file_path = Path(path)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return json.loads(file_path.read_text(encoding=encoding))
        except UnicodeDecodeError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid GeoJSON: {exc}") from exc
    raise ValueError(f"Unable to decode GeoJSON file: {last_error}") from last_error


def geojson_features(geojson: dict[str, Any]) -> list[dict[str, Any]]:
    geo_type = geojson.get("type")
    if geo_type == "FeatureCollection":
        features = geojson.get("features") or []
        if not isinstance(features, list):
            raise ValueError("GeoJSON FeatureCollection.features must be a list")
        return features
    if geo_type == "Feature":
        return [geojson]
    if geo_type in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}:
        return [{"type": "Feature", "properties": {}, "geometry": geojson}]
    raise ValueError(f"Unsupported GeoJSON type: {geo_type}")


def geometry_bbox(geometry: dict[str, Any] | None) -> list[float] | None:
    if not geometry:
        return None
    coords: list[tuple[float, float]] = []
    _collect_positions(geometry.get("coordinates"), coords)
    if not coords:
        return None
    xs = [coord[0] for coord in coords]
    ys = [coord[1] for coord in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def geometry_centroid(geometry: dict[str, Any] | None) -> dict[str, float] | None:
    bbox = geometry_bbox(geometry)
    if not bbox:
        return None
    return {"longitude": (bbox[0] + bbox[2]) / 2, "latitude": (bbox[1] + bbox[3]) / 2}


def _collect_positions(value: Any, coords: list[tuple[float, float]]) -> None:
    if not isinstance(value, list):
        return
    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        coords.append((float(value[0]), float(value[1])))
        return
    for item in value:
        _collect_positions(item, coords)


def split_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    clean = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not clean:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + chunk_size, len(clean))
        chunks.append(clean[start:end])
        if end == len(clean):
            break
        start = max(end - overlap, start + 1)
    return chunks
