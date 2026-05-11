from pathlib import Path
from typing import Any

import pandas as pd
from docx import Document
from pypdf import PdfReader


TABLE_EXTENSIONS = {".csv", ".xlsx", ".xls"}
TEXT_EXTENSIONS = {".txt", ".docx", ".pdf"}


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
