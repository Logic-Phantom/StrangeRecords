"""YouTube Studio 전용 지표 가져오기 (CSV).

YouTube Analytics API 는 Shorts 의 "피드에 표시됨(Shown in feed)" / "시청함 대 스와이프함(Viewed vs swiped away)"
지표를 제공하지 않는다 (2026-10 공식 지표 문서 기준). 이 값은 Studio → 분석 → 고급 모드에서 내보낸 CSV 를
가져올 때만 저장하며, 출처(파일명)를 함께 기록한다. 추정값을 만들지 않는다.

CSV 형식: 영상 ID 열 + 지표 열. 아래 별칭이 붙은 열만 가져오고 나머지는 무시한다.
  영상 ID: "Content" / "콘텐츠" / "video_id" / "Video" / "동영상"
  shown_in_feed: "Shown in feed" / "피드에 표시됨" / "shown_in_feed"
  viewed_rate (%): "Viewed (vs. swiped away) (%)" / "Viewed vs swiped away (%)" / "시청함(스와이프함 대비)(%)" / "viewed_rate"
  기간(선택): "start_date" / "end_date"
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.database.repositories import PerformanceRepository
from app.utils.files import now_iso

VIDEO_ID_COLUMNS = ("content", "콘텐츠", "video_id", "video", "동영상", "video id")
METRIC_ALIASES = {
    "shown_in_feed": ("shown in feed", "피드에 표시됨", "shown_in_feed"),
    "viewed_rate": ("viewed (vs. swiped away) (%)", "viewed vs swiped away (%)", "viewed vs. swiped away (%)",
                    "시청함(스와이프함 대비)(%)", "viewed_rate", "stayed to watch (%)"),
}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class ImportResult:
    rows: int = 0
    stored: int = 0
    skipped: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)


def _norm(header: str) -> str:
    return header.strip().lower().lstrip("﻿")


def _to_number(value: str) -> float | None:
    cleaned = (value or "").replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in ("-", "—"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def import_studio_csv(path: Path, repo: PerformanceRepository) -> ImportResult:
    result = ImportResult()
    source = f"manual_csv:{path.name}"
    with open(path, encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        headers = {_norm(h): h for h in (reader.fieldnames or [])}
        id_col = next((headers[h] for h in VIDEO_ID_COLUMNS if h in headers), None)
        if not id_col:
            raise ValueError(f"영상 ID 열이 없습니다 (가능: {', '.join(VIDEO_ID_COLUMNS)})")
        metric_cols = {}
        for metric, aliases in METRIC_ALIASES.items():
            col = next((headers[a] for a in aliases if a in headers), None)
            if col:
                metric_cols[metric] = col
        if not metric_cols:
            raise ValueError("가져올 지표 열이 없습니다 (Shown in feed / Viewed vs swiped away)")
        used = {id_col, *metric_cols.values(), headers.get("start_date"), headers.get("end_date")}
        result.ignored_columns = [h for h in (reader.fieldnames or []) if h not in used]
        result.metrics = list(metric_cols)
        start_col, end_col = headers.get("start_date"), headers.get("end_date")
        imported_at = now_iso()
        for row in reader:
            result.rows += 1
            vid = (row.get(id_col) or "").strip()
            if not _VIDEO_ID.match(vid):
                result.skipped.append(vid or "(빈 값)")  # 합계 행 등
                continue
            for metric, col in metric_cols.items():
                value = _to_number(row.get(col, ""))
                if value is None:
                    continue
                repo.upsert_studio_metric(vid, metric, value, source, imported_at,
                                          (row.get(start_col) or "") if start_col else "",
                                          (row.get(end_col) or "") if end_col else "")
                result.stored += 1
    return result
