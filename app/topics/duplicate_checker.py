"""주제 중복 검사 = 키워드 비교 + 텍스트 유사도 + Gemini 의미 판단.

단순 제목 비교가 아니라 "핵심 내용이 같은가" 를 판단한다.
  예) "타이타닉에서 사라진 승객" ↔ "타이타닉호 실종 승객의 미스터리" → 중복
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.gemini_client import GeminiClient, load_prompt
from app.config.settings import DuplicateConfig
from app.database.models import TopicRecord
from app.schemas import DuplicateJudgements, TopicCandidate
from app.utils.logger import get_logger

logger = get_logger("duplicate")

# 조사/어미 (긴 것부터 검사)
_PARTICLES = sorted(
    [
        "에서의", "으로의", "에게서", "이라는", "라는", "에서", "으로", "에게", "까지", "부터", "보다", "처럼",
        "이란", "란", "의", "은", "는", "이", "가", "을", "를", "에", "와", "과", "도", "로", "호", "들",
    ],
    key=len,
    reverse=True,
)
# 의미가 같은 표현 정규화
_SYNONYMS = {
    "사라진": "실종", "사라짐": "실종", "실종된": "실종", "행방불명": "실종", "증발": "실종", "사라졌다": "실종",
    "죽음": "사망", "숨진": "사망", "사망한": "사망", "숨겨진": "비밀", "비밀의": "비밀",
    "괴물": "괴수", "몬스터": "괴수", "유령선": "유령선", "배": "선박", "여객선": "선박",
}
# 어떤 주제에나 붙는 일반어 (비교에서 제외)
_STOPWORDS = {
    "미스터리", "미스테리", "이야기", "사건", "진실", "정체", "충격", "기묘한", "이상한", "놀라운", "세계", "역사",
    "전설", "비밀", "사실", "숨은", "그리고", "그런데", "대한", "관한", "있는", "없는", "the", "of", "a", "an", "and",
    "mystery", "story", "legend",
}
_TOKEN = re.compile(r"[가-힣]+|[a-zA-Z]+|\d+")


def _strip_particle(token: str) -> str:
    for p in _PARTICLES:
        if token.endswith(p) and len(token) - len(p) >= 2:
            return token[: -len(p)]
    return token


def normalize_token(token: str) -> str:
    token = token.lower()
    if token in _SYNONYMS:
        return _SYNONYMS[token]
    stripped = _strip_particle(token)
    return _SYNONYMS.get(stripped, stripped)


def extract_keywords(*texts: str, extra: list[str] | None = None) -> set[str]:
    words: set[str] = set()
    for text in list(texts) + list(extra or []):
        for raw in _TOKEN.findall(text or ""):
            token = normalize_token(raw)
            if len(token) >= 2 and token not in _STOPWORDS:
                words.add(token)
    return words


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 2 and long_.startswith(short) and len(short) / len(long_) >= 0.5


def keyword_overlap(a: set[str], b: set[str]) -> float:
    """overlap coefficient (퍼지 토큰 매칭): |A∩B| / min(|A|,|B|)"""
    if not a or not b:
        return 0.0
    matched = sum(1 for x in a if any(_token_match(x, y) for y in b))
    return matched / min(len(a), len(b))


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^가-힣a-zA-Z0-9]", "", text.lower())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def text_similarity(a: str, b: str) -> float:
    """문자 bigram Dice 계수."""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


@dataclass
class DuplicateResult:
    candidate: TopicCandidate
    duplicate: bool
    score: float = 0.0
    similar_to: str = ""
    reason: str = ""
    method: str = ""
    shortlist: list[tuple[float, TopicRecord]] = field(default_factory=list)


class DuplicateChecker:
    def __init__(self, config: DuplicateConfig, client: GeminiClient | None = None):
        self.config = config
        self.client = client

    @staticmethod
    def _candidate_keywords(c: TopicCandidate) -> set[str]:
        return extract_keywords(c.title, c.topic, extra=c.keywords)

    @staticmethod
    def _record_keywords(r: TopicRecord) -> set[str]:
        return extract_keywords(r.title, r.topic, extra=r.keywords)

    def score(self, candidate: TopicCandidate, record: TopicRecord) -> tuple[float, float]:
        kw = keyword_overlap(self._candidate_keywords(candidate), self._record_keywords(record))
        txt = max(
            text_similarity(candidate.title, record.title),
            text_similarity(candidate.topic, record.topic),
        )
        return kw, txt

    def lexical_check(self, candidate: TopicCandidate, history: list[TopicRecord]) -> DuplicateResult:
        scored: list[tuple[float, TopicRecord]] = []
        for record in history:
            kw, txt = self.score(candidate, record)
            if kw >= self.config.keyword_threshold or txt >= self.config.text_threshold:
                method = "keyword" if kw >= self.config.keyword_threshold else "text"
                return DuplicateResult(
                    candidate, True, max(kw, txt), record.title,
                    f"키워드 겹침 {kw:.2f}, 텍스트 유사도 {txt:.2f}", method,
                )
            scored.append((max(kw, txt), record))
        scored.sort(key=lambda x: x[0], reverse=True)
        shortlist = [s for s in scored if s[0] >= self.config.semantic_min_score][: self.config.semantic_top_k]
        return DuplicateResult(candidate, False, scored[0][0] if scored else 0.0, shortlist=shortlist)

    def check(self, candidates: list[TopicCandidate], history: list[TopicRecord]) -> list[DuplicateResult]:
        results = [self.lexical_check(c, history) for c in candidates]

        # 후보끼리도 중복이면 뒤의 것을 제거
        for i, res in enumerate(results):
            if res.duplicate:
                continue
            for prev in results[:i]:
                if prev.duplicate:
                    continue
                kw = keyword_overlap(self._candidate_keywords(res.candidate), self._candidate_keywords(prev.candidate))
                if kw >= self.config.keyword_threshold:
                    res.duplicate, res.similar_to, res.method = True, prev.candidate.title, "candidate"
                    res.reason = "같은 회차 후보끼리 중복"
                    break

        if self.config.semantic_check and self.client and history:
            self._semantic_check([r for r in results if not r.duplicate], history)

        for r in results:
            if r.duplicate:
                logger.info("중복 제외: '%s' ≈ '%s' (%s: %s)", r.candidate.title, r.similar_to, r.method, r.reason)
        return results

    def _semantic_check(self, pending: list[DuplicateResult], history: list[TopicRecord]) -> None:
        if not pending:
            return
        recent = history[: self.config.semantic_top_k]
        blocks: list[str] = []
        for idx, res in enumerate(pending):
            compare: list[TopicRecord] = [rec for _, rec in res.shortlist]
            for rec in recent:
                if rec not in compare:
                    compare.append(rec)
            compare = compare[: self.config.semantic_top_k * 2]
            lines = "\n".join(f"    - {rec.title} :: {rec.topic}" for rec in compare) or "    - (없음)"
            blocks.append(f"[후보 index={idx}] {res.candidate.title} :: {res.candidate.topic}\n  비교 대상:\n{lines}")

        try:
            judgement = self.client.generate_json(  # type: ignore[union-attr]
                load_prompt("duplicate_prompt", pairs="\n\n".join(blocks)), DuplicateJudgements, temperature=0.1
            )
        except Exception as exc:  # 의미 판단 실패 시 lexical 결과만 사용 (작업 전체 실패 방지)
            logger.warning("Gemini 의미 기반 중복 판단 실패, 키워드/텍스트 결과만 사용: %s", exc)
            return

        for item in judgement.results:
            if 0 <= item.index < len(pending) and item.duplicate:
                res = pending[item.index]
                res.duplicate, res.similar_to, res.reason, res.method = True, item.similar_to, item.reason, "gemini"
