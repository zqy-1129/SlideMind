from datetime import datetime

from app.services.normalization import normalize_record


def test_normalize_insar_record_with_chinese_fields():
    row = {"监测点编号": "P001", "观测时间": "2025-01-02", "经度": "110.1", "纬度": "30.2", "累计形变": "35.6"}

    normalized = normalize_record(row)

    assert normalized["point_id"] == "P001"
    assert normalized["timestamp"] == datetime(2025, 1, 2)
    assert normalized["longitude"] == 110.1
    assert normalized["latitude"] == 30.2
    assert normalized["displacement"] == 35.6


def test_normalize_water_level_record():
    row = {"水位站": "三峡库区", "时间": "2025/02/03 10:30:00", "库水位": 165.8}

    normalized = normalize_record(row)

    assert normalized["station_name"] == "三峡库区"
    assert normalized["timestamp"] == datetime(2025, 2, 3, 10, 30)
    assert normalized["water_level"] == 165.8

