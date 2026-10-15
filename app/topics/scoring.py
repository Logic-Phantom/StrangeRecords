"""주제 후보 점수 산정 (설정 가능한 가중치, 설명 가능한 규칙 기반).

모든 구성 요소는 0~1 로 정규화한다. 값이 없는(None) 요소는 가중치 합에서 빠지고 남은 요소의 가중치로 다시 정규화한다.

    base  = Σ(weight_i × component_i) / Σ(weight_i)          (값이 있는 요소만)
    final = base − saturation_penalty × saturation − duplicate_penalty × near_duplicate

구성 요소
- (결측) 같은 회차의 다른 후보에는 신호가 있는데 이 후보만 없으면 trend/velocity 를 회차 중앙값으로 채운다
- trend       외부 관심도: 위키백과 최근 7일 일평균 조회수 / YouTube 최근 영상 시간당 조회수 (log 정규화) × 신선도 가중치
- velocity    관심도 증가 속도: 위키백과 최근 7일 평균 ÷ 이전 기간 평균 (1배=0, 2배 이상=1)
- channel_fit 채널 주제 관련성: 카테고리 안에서 생성된 후보 기본 0.6 + 채널 단어 일치 × 0.4
- performance 채널 과거 성과: 같은 카테고리의 평균 시청 비율·조회수를 채널 전체와 비교 (표본이 적으면 채널 평균 쪽으로 축소, 부족하면 None)
- novelty     신규성: 1 − (기존 주제와의 키워드/텍스트 유사도)
- story       40~60초 이야기 구조: Gemini 가 제시한 전개(의문 → 배경 → 단서/반전 → 회수) 단계 수
- evidence    사실 확인 가능성: truth_status + 위키백과 문서 존재 여부
패널티
- saturation     최근 30일 같은 주제 YouTube 영상 비율 (0~1)
- near_duplicate 기존 주제와 중복 판정 기준에 가까운 정도 (0~1)

안전장치: 채널 관련성 < min_channel_fit 또는 사실 확인 가능성 < min_evidence_for_trend 이면
trend / velocity 는 반영하지 않는다 → 화제성만 높은 무관한/허구 주제가 채택되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config.settings import Settings
from app.schemas import TopicCandidate
from app.topics.duplicate_checker import DuplicateResult
from app.trends.base import TrendSignal, freshness_weight
from app.trends.collector import channel_lexicon, channel_relevance

TRUTH_EVIDENCE = {"FACT": 1.0, "UNCONFIRMED": 0.6, "LEGEND": 0.7, "URBAN_LEGEND": 0.5, "FICTION": 0.2}


@dataclass
class TopicScore:
    result: DuplicateResult
    components: dict[str, float | None] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    final: float = 0.0
    reasons: list[str] = field(default_factory=list)
    trend_sources: list[str] = field(default_factory=list)
    selected: bool = False
    gated: bool = False                 # 사실 확인/채널 관련성 기준으로 외부 관심도를 반영하지 않음
    imputed: list[str] = field(default_factory=list)

    @property
    def candidate(self) -> TopicCandidate:
        return self.result.candidate

    @property
    def has_external(self) -> bool:
        """외부 트렌드 신호가 점수에 반영되었는가 (성과 지수는 같은 카테고리 후보 사이에서 같으므로 순위에 영향 없음)."""
        real = [k for k in ("trend", "velocity") if self.components.get(k) is not None and k not in self.imputed]
        return bool(real) or self.penalties.get("saturation", 0) > 0

    def to_row(self) -> dict[str, Any]:
        return {
            "title": self.candidate.title, "topic": self.candidate.topic, "category": self.candidate.category,
            "selected": self.selected, "duplicate": self.result.duplicate,
            "final_score": round(self.final, 4) if not self.result.duplicate else None,
            "components": {k: (round(v, 4) if v is not None else None) for k, v in self.components.items()}
            | {f"penalty_{k}": round(v, 4) for k, v in self.penalties.items()},
            "weights": self.weights_used, "reasons": self.reasons, "trend_sources": self.trend_sources,
        }


def story_score(candidate: TopicCandidate) -> float:
    beats = [b for b in candidate.story_beats if b.strip()]
    if beats:
        return {1: 0.4, 2: 0.7}.get(len(beats), 1.0)
    # 이전 형식 후보(story_beats 없음): Hook 아이디어와 요약이 있으면 보통
    return 0.5 if candidate.hook_idea.strip() and candidate.summary.strip() else 0.3


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def score_candidate(
    result: DuplicateResult,
    signals: list[TrendSignal],
    settings: Settings,
    category_prior: float | None = None,
    now: datetime | None = None,
) -> TopicScore:
    cfg = settings.trends.scoring
    dup_cfg = settings.duplicate
    now = now or datetime.now().astimezone()
    c = result.candidate
    score = TopicScore(result=result)
    comp = score.components
    reasons = score.reasons

    comp["novelty"] = _clamp(1 - result.score)
    comp["story"] = story_score(c)
    reasons.append(f"신규성 {comp['novelty']:.2f} (기존 주제 최대 유사도 {result.score:.2f})")

    # 사실 확인 가능성: truth_status + 위키백과 문서 존재
    evidence = TRUTH_EVIDENCE.get(c.truth_status, 0.5)
    wiki = [s for s in signals if s.provider == "wikipedia" and s.kind == "article_pageviews"]
    if wiki:
        found = any(s.status == "ok" for s in wiki)
        evidence *= 1.0 if found else 0.7
        reasons.append("위키백과 문서 확인" if found else "위키백과 문서 없음 → 사실 확인 가능성 낮춤")
    comp["evidence"] = evidence

    lexicon = channel_lexicon(settings, settings.category(c.category))
    rel = channel_relevance(f"{c.title} {c.topic} {' '.join(c.keywords)}", lexicon)
    comp["channel_fit"] = 0.6 + 0.4 * rel
    comp["performance"] = category_prior
    if category_prior is not None:
        reasons.append(f"카테고리 과거 성과 지수 {category_prior:.2f} (0.5=채널 평균)")

    # 외부 관심도
    ok = [s for s in signals if s.status == "ok"]
    trend_vals = [
        (s.interest_normalized or 0.0) * freshness_weight(s.age_hours(now), cfg.freshness_half_life_hours)
        for s in ok if s.interest_normalized is not None
    ]
    velocities = [s.velocity for s in ok if s.velocity is not None]
    comp["trend"] = max(trend_vals) if trend_vals else None
    comp["velocity"] = _clamp(max(velocities) - 1.0) if velocities else None
    score.trend_sources = sorted({f"{s.provider}:{s.kind}" for s in ok})
    gate = []
    if comp["channel_fit"] < cfg.min_channel_fit:
        gate.append(f"채널 관련성 {comp['channel_fit']:.2f} < {cfg.min_channel_fit}")
    if evidence < cfg.min_evidence_for_trend:
        gate.append(f"사실 확인 가능성 {evidence:.2f} < {cfg.min_evidence_for_trend}")
    if gate and (comp["trend"] is not None or comp["velocity"] is not None):
        comp["trend"] = comp["velocity"] = None
        reasons.append("외부 관심도 미반영: " + ", ".join(gate))
    elif comp["trend"] is not None:
        reasons.append(f"외부 관심도 {comp['trend']:.2f}" + (f", 증가 속도 {comp['velocity']:.2f}" if comp["velocity"] is not None else ""))

    # 패널티
    sats = [s.saturation for s in ok if s.saturation is not None]
    saturation = max(sats) if sats else 0.0
    span = max(dup_cfg.text_threshold - dup_cfg.semantic_min_score, 0.01)
    near_dup = _clamp((result.score - dup_cfg.semantic_min_score) / span) if result.score >= dup_cfg.semantic_min_score else 0.0
    score.penalties = {"saturation": saturation * cfg.saturation_penalty, "duplicate": near_dup * cfg.duplicate_penalty}
    if saturation:
        reasons.append(f"최근 같은 주제 영상 과밀도 {saturation:.2f}")

    score.gated = bool(gate)
    finalize(score, cfg)
    return score


def finalize(score: TopicScore, cfg) -> None:
    comp = score.components
    weights = {k: w for k, w in cfg.weights.items() if comp.get(k) is not None and w > 0}
    total_w = sum(weights.values()) or 1.0
    base = sum(w * comp[k] for k, w in weights.items()) / total_w  # type: ignore[operator]
    score.weights_used = {k: round(w / total_w, 4) for k, w in weights.items()}
    score.final = base - sum(score.penalties.values())


def score_candidates(
    results: list[DuplicateResult],
    signals_by_candidate: dict[int, list[TrendSignal]],
    settings: Settings,
    category_prior: float | None = None,
    now: datetime | None = None,
) -> list[TopicScore]:
    scores = [score_candidate(r, signals_by_candidate.get(i, []), settings, category_prior, now) for i, r in enumerate(results)]
    # 결측 보정: 같은 회차에서 일부 후보만 외부 신호가 있으면, 신호가 없는 후보(조회 실패/문서 없음)는
    # 같은 회차 중앙값으로 채운다. 결측 항목을 가중치에서 빼기만 하면 "데이터가 없는 후보"가 오히려 유리해지기 때문.
    # (사실 확인/채널 관련성 기준으로 외부 관심도를 막은 후보는 채우지 않는다)
    fresh = [s for s in scores if not s.result.duplicate]
    for key in ("trend", "velocity"):
        observed = sorted(s.components[key] for s in fresh if s.components.get(key) is not None)
        if not observed:
            continue
        mid = observed[len(observed) // 2] if len(observed) % 2 else (observed[len(observed) // 2 - 1] + observed[len(observed) // 2]) / 2
        for s in fresh:
            if s.components.get(key) is None and not s.gated:
                s.components[key] = mid
                s.imputed.append(key)
    for s in fresh:
        if s.imputed:
            s.reasons.append(f"외부 신호 없음 → {', '.join(s.imputed)} 를 같은 회차 중앙값으로 대체")
            finalize(s, settings.trends.scoring)
    for s in scores:
        if s.result.duplicate:
            s.final = float("-inf")
            s.reasons.insert(0, f"중복 제외 ({s.result.method}: {s.result.similar_to})")
    return scores
