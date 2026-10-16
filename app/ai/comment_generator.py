"""영상의 실제 제목·최종 대본·확인된 내용에 맞는 최상위 댓글 본문을 만들고 검수한다.

- Gemini 가 있으면 대본/자료/explainer 를 근거로 생성하고, 검수 규칙(validate_comment)을 어기면 피드백과 함께 재요청한다.
- Gemini 를 쓰지 않거나 실패하면 대본에 이미 들어 있는 댓글 질문(comment_prompt)과 핵심 단서로 구성한다.
- 검수를 통과하지 못한 댓글은 등록하지 않는다 (빈 댓글, 영상과 무관한 댓글, 홍보/링크, 허위 경험, 반복 댓글, 미완성 문구).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.ai.gemini_client import ContentValidationError, GeminiClient, load_prompt
from app.ai.metadata_generator import years
from app.ai.title_selector import CLICKBAIT
from app.config.settings import Settings
from app.schemas import Explainer, ResearchNotes
from app.topics.duplicate_checker import _token_match, extract_keywords, text_similarity
from app.utils.logger import get_logger

logger = get_logger("comment")

COMMENT_TYPES = ("clue_question", "hypothesis_compare", "open_question", "perspective")

_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(com|net|org|kr|ly|io|be)\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"[\[\]{}<>]|TODO|TBD|OOO|○○|XX|가설 ?[AB]\b|\.\.\.$")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
PROMO = ("구독", "좋아요", "알림 설정", "알림설정", "링크", "광고", "협찬", "할인", "이벤트", "경품", "프로필", "채널 홍보")
FAKE_FOLLOWUP = ("후속편", "다음 편", "다음 영상", "다음 이야기", "2부", "다음 시간", "곧 공개")
FAKE_EXPERIENCE = ("제가 직접", "직접 가 봤", "직접 가봤", "직접 봤", "직접 보았", "저도 시청", "시청자로서", "구독자로서",
                   "영상 잘 봤", "보고 왔", "친구가", "제보", "저희 할머니", "제 경험")


class GeneratedComment(BaseModel):
    comment_type: str = "open_question"
    text: str
    grounding: list[str] = Field(default_factory=list)


@dataclass
class CommentDraft:
    text: str
    comment_type: str
    generator: str                      # gemini | fallback
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.problems


def validate_comment(text: str, script: str, title: str, settings: Settings,
                     previous_texts: list[str] | None = None) -> list[str]:
    """등록 전 검수. 문제가 있으면 이유 목록 (빈 목록이면 통과)."""
    cfg = settings.comments
    text = (text or "").strip()
    problems: list[str] = []
    if not text:
        return ["댓글 본문이 비어 있다."]
    if len(text) < cfg.min_chars:
        problems.append(f"댓글이 너무 짧다 ({len(text)}자 < {cfg.min_chars}자).")
    if len(text) > cfg.max_chars:
        problems.append(f"댓글이 너무 길다 ({len(text)}자 > {cfg.max_chars}자).")
    if _PLACEHOLDER.search(text):
        problems.append("대괄호·빈칸 등 미완성 문구가 남아 있다.")
    if _URL.search(text) or "#" in text or "@" in text:
        problems.append("링크·해시태그·멘션은 넣지 않는다.")
    if _EMOJI.search(text):
        problems.append("이모지는 넣지 않는다.")
    for words, reason in ((PROMO, "홍보/구독 요청"), (FAKE_FOLLOWUP, "존재하지 않는 후속편/이벤트 언급"),
                          (FAKE_EXPERIENCE, "허위 경험 또는 시청자/제3자인 척하는 표현")):
        hit = [w for w in words if w in text]
        if hit:
            problems.append(f"{reason} 금지: {', '.join(hit)}")
    bait = [p for p in (*CLICKBAIT, *settings.script.banned_phrases) if p and p in text]
    if bait:
        problems.append(f"낚시성 표현 금지: {', '.join(bait)}")
    if cfg.require_question and "?" not in text:
        problems.append("시청자 의견을 묻는 질문(?)이 없다.")
    # 영상과 관련 있는가: 대본/제목의 핵심어와 충분히 겹쳐야 한다
    source_words = extract_keywords(script, title)
    matched = {w for w in extract_keywords(text) if any(_token_match(w, s) for s in source_words)}
    if len(matched) < cfg.min_keyword_overlap:
        problems.append(f"영상 내용과 관련된 핵심어가 부족하다 (겹친 단어 {len(matched)}개 < {cfg.min_keyword_overlap}개).")
    # 대본에 없는 연도는 쓰지 않는다
    extra_years = years(text) - years(script + " " + title)
    if extra_years:
        problems.append(f"대본에 없는 연도가 있다: {', '.join(sorted(extra_years))}")
    for prev in previous_texts or []:
        if text_similarity(text, prev) >= cfg.max_similarity_to_previous:
            problems.append("이전에 단 자동 댓글과 거의 같다 (반복 댓글 금지).")
            break
    return problems


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items if i) or "(없음)"


def _josa(word: str, with_batchim: str, without: str) -> str:
    """앞 글자 받침에 맞는 조사 (예: 가설+과, 이야기+와)."""
    last = word.strip()[-1:] if word.strip() else ""
    if "가" <= last <= "힣":
        return with_batchim if (ord(last) - 0xAC00) % 28 else without
    return without


def fallback_comment(title: str, comment_prompt: str, explainer: Explainer | None) -> tuple[str, str]:
    """Gemini 없이 대본에 이미 있는 질문으로 댓글을 구성한다 (영상 내용 그대로, 새 사실 추가 없음)."""
    question = (comment_prompt or "").strip()
    ex = explainer or Explainer()
    if len(ex.hypotheses) >= 2:
        a, b = ex.hypotheses[0].name.strip(), ex.hypotheses[1].name.strip()
        lead = f"이번 영상에서는 {a}{_josa(a, '과', '와')} {b}, 두 가지 설명을 살펴봤습니다."
        kind = "hypothesis_compare"
    elif ex.key_clues:
        clue = ex.key_clues[0].strip().rstrip(".")
        lead = f"이번 이야기에서 가장 설명하기 어려웠던 단서는 {clue}{_josa(clue, '이었', '였')}습니다."
        kind = "clue_question"
    else:
        lead = f"'{title.strip()}' 이야기를 정리하면서 아직 풀리지 않은 부분이 많다고 느꼈습니다."
        kind = "open_question"
    if question and not question.endswith("?"):
        question = question.rstrip(".") + "?"
    tail = " 그렇게 생각하신 근거도 궁금합니다." if question else ""
    return f"{lead} {question}{tail}".strip(), kind


class CommentGenerator:
    def __init__(self, settings: Settings, client: GeminiClient | None = None):
        self.settings = settings
        self.client = client

    def generate(self, title: str, script: str, comment_prompt: str = "", research: ResearchNotes | None = None,
                 explainer: Explainer | None = None, previous_texts: list[str] | None = None) -> CommentDraft:
        cfg = self.settings.comments
        ex = explainer or Explainer()
        previous_texts = previous_texts or []

        def check(text: str) -> list[str]:
            return validate_comment(text, script, title, self.settings, previous_texts)

        if self.client is not None and cfg.use_gemini:
            facts = research.facts if research else []
            prompt = load_prompt(
                "comment_prompt",
                channel_name=self.settings.app.name, title=title, script=script,
                confirmed=_lines([f.statement for f in facts if f.status == "FACT"] + ex.confirmed_facts),
                unconfirmed=_lines([f.statement for f in facts if f.status != "FACT"] + ex.unconfirmed_claims),
                clues=_lines(ex.key_clues),
                hypotheses=_lines([f"{h.name}: {h.evidence} / 약점: {h.weakness}".strip(" :/") for h in ex.hypotheses]),
                unresolved=_lines(ex.unresolved + (research.open_questions if research else [])),
                previous_comments=_lines(previous_texts[:10]),
                min_chars=cfg.min_chars, max_chars=cfg.max_chars, tone=cfg.tone,
                language="한국어" if cfg.language.startswith("ko") else cfg.language,
            )

            def _validate(result: GeneratedComment) -> None:
                problems = check(result.text)
                if problems:
                    raise ContentValidationError("\n".join(f"- {p}" for p in problems))

            try:
                result = self.client.generate_json(prompt, GeneratedComment, validate=_validate, temperature=0.7)
                kind = result.comment_type if result.comment_type in COMMENT_TYPES else "open_question"
                return CommentDraft(result.text.strip(), kind, "gemini")
            except Exception as exc:
                logger.warning("Gemini 댓글 생성 실패 → 대본의 댓글 질문으로 구성: %s", str(exc).splitlines()[0][:200])

        text, kind = fallback_comment(title, comment_prompt, ex)
        return CommentDraft(text, kind, "fallback", check(text))
