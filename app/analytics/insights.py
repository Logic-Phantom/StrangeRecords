"""성과 데이터를 다음 제작(카테고리 선택, 주제 점수, 대본 프롬프트)에 반영할 수 있는 형태로 요약한다.

과최적화 방지
- 판단 가능한 영상이 min_samples 미만이면 아무것도 반영하지 않는다 (None / "데이터 부족").
- 카테고리 성과 지수는 채널 평균(0.5) 쪽으로 축소한다: (n × 평균 + k × 0.5) / (n + k)
- 프롬프트에는 "참고"로만 넣고, 그룹별 표본 수와 신뢰도를 함께 적는다.
- 성과는 같은 카테고리 중앙값 대비 상대값으로 비교한다 (트렌드/카테고리 효과와 형식 효과를 섞지 않기 위해).
"""

from __future__ import annotations

from datetime import datetime

from app.analytics.report import build_video_rows, confidence_label, group_stats
from app.config.settings import Settings
from app.database.db import Database
from app.utils.logger import get_logger

logger = get_logger("analytics.insights")


def _pct_ranks(values: list[float | None]) -> list[float | None]:
    present = sorted(v for v in values if v is not None)
    if len(present) < 2:
        return [0.5 if v is not None else None for v in values]
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        below = sum(1 for p in present if p < v)
        equal = sum(1 for p in present if p == v)
        out.append((below + (equal - 1) / 2) / (len(present) - 1))
    return out


class PerformanceInsights:
    def __init__(self, db: Database, settings: Settings, rows: list[dict] | None = None, now: datetime | None = None):
        self.settings = settings
        self.cfg = settings.analytics
        self._rows = rows
        self._db = db
        self._now = now

    @property
    def rows(self) -> list[dict]:
        if self._rows is None:
            try:
                self._rows = build_video_rows(self._db, self.settings, now=self._now)
            except Exception as exc:  # 성과 데이터가 없어도 제작은 계속
                logger.warning("성과 데이터 읽기 실패 → 반영하지 않음: %s", exc)
                self._rows = []
        return self._rows

    @property
    def ready(self) -> list[dict]:
        return [r for r in self.rows if r["ready"]]

    @property
    def sufficient(self) -> bool:
        return len(self.ready) >= self.cfg.min_samples

    def video_scores(self) -> dict[str, float]:
        """영상별 채널 내 상대 성과 0~1 (평균 시청 비율 순위 50% + 72h 조회수 순위 50%)."""
        ready = self.ready
        avp = _pct_ranks([r["average_view_percentage"] for r in ready])
        views = _pct_ranks([r["views_72h"] if r["views_72h"] is not None else r["views"] for r in ready])
        scores = {}
        for r, a, v in zip(ready, avp, views):
            parts = [x for x in (a, v) if x is not None]
            if parts:
                scores[r["job_id"]] = sum(parts) / len(parts)
        return scores

    def category_prior(self, category: str) -> float | None:
        if not self.sufficient:
            return None
        scores = self.video_scores()
        values = [scores[r["job_id"]] for r in self.ready if r["category"] == category and r["job_id"] in scores]
        k = self.cfg.shrinkage_k
        return (sum(values) + k * 0.5) / (len(values) + k)

    def category_priors(self) -> dict[str, float]:
        if not self.sufficient:
            return {}
        return {c.id: self.category_prior(c.id) or 0.5 for c in self.settings.categories}

    def prompt_context(self, category: str | None = None) -> str:
        """대본/주제 프롬프트에 넣을 성과 요약. 데이터가 부족하면 그렇게 적는다."""
        if not self.sufficient:
            return f"(성과 데이터 부족: 판단 가능한 영상 {len(self.ready)}개 < {self.cfg.min_samples}개 → 기본 원칙대로 작성)"
        lines = [f"판단 가능한 최근 영상 {len(self.ready)}개 기준 (같은 카테고리 중앙값 대비 평균 시청 비율 차이, 참고용):"]
        for key, label in (("hook_type", "도입 유형"), ("loop_ending_enabled", "루프 엔딩"), ("duration_bucket", "영상 길이")):
            for s in group_stats(self.rows, key, self.cfg.min_samples):
                if s["mean_rel_avp"] is None:
                    continue
                lines.append(f"- {label} '{s['value']}': {s['mean_rel_avp']:+.1f}%p (n={s['n']}, 신뢰도 {s['confidence']})")
        shares = sorted((r for r in self.ready if r["shares_per_1k"] is not None), key=lambda r: r["shares_per_1k"], reverse=True)[:2]
        for r in shares:
            lines.append(f"- 공유가 상대적으로 많았던 영상: {r['title'][:40]} (공유 {r['shares_per_1k']:.1f}/천회)")
        low = [r for r in self.ready if any(d["code"] == "low_retention" for d in r["diagnosis"])]
        if category:
            low = [r for r in low if r["category"] == category] or low
        if low:
            lines.append(f"- 시청 지속이 낮았던 영상 {len(low)}개: 배경 설명을 줄이고 장면마다 새 정보를 넣을 것")
        lines.append("표본이 적은 항목은 신뢰도가 낮다. 특정 유형을 강제하지 말고 사실 정확성과 이야기 완결성을 우선한다.")
        return "\n".join(lines)

    def summary(self) -> dict:
        return {
            "ready": len(self.ready), "sufficient": self.sufficient,
            "confidence": confidence_label(len(self.ready), self.cfg.min_samples),
            "category_priors": {k: round(v, 3) for k, v in self.category_priors().items()},
        }
