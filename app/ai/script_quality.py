"""대본 품질 평가: 도입부, 정보 전개, 반복, 사실/추측 구분, 최근 대본과의 유사도, 자연스러운 루프 엔딩.

기존 validate_script(분량/금지 표현/사실 고지)는 그대로 두고, 여기서는 추가 항목만 평가한다.
- problems: 재생성이 필요한 문제 → ContentValidationError 로 피드백과 함께 재요청 (json_retries 횟수 제한)
- warnings: 기록만 하는 항목
- 루프: 품질 점수(0~1)가 기준 미만이면 루프를 쓰지 않은 것으로 기록한다 (대본은 일반 결말로 완결되어 있어야 함).
  루프 효과가 시청 지속을 높인다고 가정하지 않는다 → production_metadata 에 기록하고 Analytics 로 검증.
추가 Gemini 호출 없이 규칙으로 평가한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.gemini_client import ContentValidationError
from app.ai.validator import normalize_for_compare, speech_chars
from app.config.settings import ScriptConfig
from app.schemas import ScriptDraft
from app.topics.duplicate_checker import extract_keywords, keyword_overlap, text_similarity

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
# 추측/전승을 나타내는 표현 (FACT 가 아닌 주제는 하나 이상 있어야 단정하지 않은 것으로 본다)
HEDGES = ("전해", "추정", "주장", "알려져", "확인되지", "확인된 바", "가설", "라는 이야기", "것으로 보", "설이", "추측",
          "논란", "의견", "믿", "라고 합니다", "는 말이", "이라고도", "기록에 따르면", "밝혀지지")
_CLIFFHANGER = ("다음 편", "다음 영상", "2부", "계속됩니다", "to be continued", "다음 이야기")
_COMPLETE = re.compile(r"[.!?]$")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


def is_complete_sentence(sentence: str) -> bool:
    s = sentence.strip()
    if not s or s.endswith((":", "…", "...", ",")):
        return False
    if any(c in s.lower() for c in _CLIFFHANGER):
        return False
    return bool(_COMPLETE.search(s))


def loop_quality(hook: str, ending: str, script: str) -> tuple[float, str]:
    """마지막 문장(ending) → 첫 문장(hook) 으로 돌아갈 때의 자연스러움 (0~1) 과 설명.

    - 필수: ending 이 대본의 마지막 문장이고, 끝맺은 완결 문장이어야 한다 (미완성/콜론/다음 편 유도 = 0)
    - 연결 고리(50%): 첫 문장과 핵심 단어(대상/시간/장소)를 공유 (키워드 겹침 0.5 이상이면 만점)
    - 반복 아님(30%): 첫 문장을 그대로 되풀이하지 않음
    - 회수(20%): 시청자에게 던지는 질문으로 끝나지 않고 사건의 핵심을 서술
    """
    sentences = split_sentences(script)
    if not ending.strip() or not sentences:
        return 0.0, "ending 없음"
    last = sentences[-1]
    if normalize_for_compare(ending)[:10] not in normalize_for_compare(last):
        return 0.0, "루프 모드인데 마지막 문장이 ending 이 아님"
    if not is_complete_sentence(last):
        return 0.0, "마지막 문장이 미완성(콜론/말줄임/다음 편 유도)"
    overlap = keyword_overlap(extract_keywords(ending), extract_keywords(hook))
    anchor = min(overlap / 0.5, 1.0)
    verbatim = text_similarity(ending, hook) >= 0.8
    question = last.rstrip().endswith("?")
    score = 0.5 * anchor + 0.3 * (0.0 if verbatim else 1.0) + 0.2 * (0.0 if question else 1.0)
    notes = [f"첫 문장과 핵심어 겹침 {overlap:.2f}"]
    if verbatim:
        notes.append("첫 문장 단순 반복")
    if question:
        notes.append("질문으로 끝남(회수 아님)")
    return round(score, 3), ", ".join(notes)


@dataclass
class ScriptQuality:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    loop_score: float | None = None
    loop_note: str = ""
    max_similarity: float = 0.0
    similar_to: str = ""
    hook_similarity: float = 0.0
    hook_seconds: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def evaluate_script(draft: ScriptDraft, cfg: ScriptConfig, previous: list[dict] | None = None) -> ScriptQuality:
    q = ScriptQuality()
    qc = cfg.quality
    sentences = split_sentences(draft.script)

    # 도입부: 첫 문장이 hook 이고 짧게 핵심을 던지는가
    q.hook_seconds = round(speech_chars(draft.hook) / max(cfg.chars_per_second, 0.1), 1)
    if q.hook_seconds > qc.max_hook_seconds:
        q.warnings.append(f"첫 문장이 길다 ({q.hook_seconds}초 > {qc.max_hook_seconds}초): 핵심 의문을 앞쪽 3초 안에")
    if draft.hook.strip().endswith((":", "…", "...")):
        q.problems.append("hook 이 미완성 문장이다. 첫 문장만 들어도 이해되는 완결된 문장으로 써라.")

    # 결말: 미완성 문장/다음 편 유도 금지
    if sentences and not is_complete_sentence(sentences[-1]):
        q.problems.append("마지막 문장이 미완성(콜론, 말줄임표, '다음 편' 유도)이다. 결말을 완결된 문장으로 써라.")

    # 불필요한 반복
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            a, b = sentences[i], sentences[j]
            if speech_chars(a) >= 8 and speech_chars(b) >= 8 and text_similarity(a, b) >= qc.max_sentence_repeat:
                q.problems.append(f"같은 내용 반복: '{a[:20]}' / '{b[:20]}'. 장면마다 새 정보를 넣어라.")
                break
        else:
            continue
        break

    # 사실과 추측 구분
    if draft.truth_status != "FACT" and not any(h in draft.script for h in HEDGES):
        q.problems.append(
            f"truth_status 가 {draft.truth_status} 인데 대본에 추측/전승 표현이 없다."
            " '~라고 전해집니다', '~로 추정됩니다', '~라는 주장도 있습니다' 처럼 단정하지 않게 써라."
        )

    # 정보 전개
    beats = [b for b in draft.story_beats if b.strip()]
    if draft.story_beats and len(beats) < 3:
        q.warnings.append(f"전개 단계 {len(beats)}개 (의문 → 배경 → 단서/반전 → 회수 권장)")

    # 최근 대본과의 유사도 (같은 이야기의 재탕 / 같은 도입 문장 반복 방지)
    for prev in (previous or [])[: qc.compare_recent]:
        sim = text_similarity(draft.script, prev.get("script", ""))
        if sim > q.max_similarity:
            q.max_similarity, q.similar_to = sim, prev.get("title", "")
        q.hook_similarity = max(q.hook_similarity, text_similarity(draft.hook, prev.get("hook", "")))
    q.max_similarity = round(q.max_similarity, 3)
    q.hook_similarity = round(q.hook_similarity, 3)
    if q.max_similarity >= qc.max_script_similarity:
        q.problems.append(f"최근 영상('{q.similar_to}') 대본과 너무 비슷하다 (유사도 {q.max_similarity:.2f}). 다른 관점·새 정보로 써라.")
    if q.hook_similarity >= qc.max_hook_similarity:
        q.problems.append(f"첫 문장이 최근 영상의 첫 문장과 거의 같다 (유사도 {q.hook_similarity:.2f}). 다른 도입 문장을 써라.")

    # 자연스러운 루프 엔딩
    if draft.loop:
        q.loop_score, q.loop_note = loop_quality(draft.hook, draft.ending, draft.script)
    return q


def validate_script_quality(draft: ScriptDraft, cfg: ScriptConfig, previous: list[dict] | None = None) -> ScriptQuality:
    """재생성이 필요한 문제가 있으면 ContentValidationError (GeminiClient.generate_json 이 피드백과 함께 재요청)."""
    q = evaluate_script(draft, cfg, previous)
    if q.problems:
        raise ContentValidationError("\n".join(f"- {p}" for p in q.problems))
    return q


def apply_loop_decision(draft: ScriptDraft, q: ScriptQuality, cfg: ScriptConfig) -> ScriptDraft:
    """루프 사용 여부를 확정해 draft 에 기록한다. 자연스럽지 않으면 일반 결말로 처리(loop=False)."""
    loop_cfg = cfg.loop_ending
    draft.script_similarity = q.max_similarity
    if not draft.loop:
        draft.loop_note = draft.loop_note or "일반 결말"
        return draft
    if not loop_cfg.enabled or loop_cfg.mode == "off":
        draft.loop, draft.loop_note = False, "루프 엔딩 비활성 (script.loop_ending)"
        return draft
    draft.loop_quality = q.loop_score
    if loop_cfg.require_semantic_continuity and (q.loop_score or 0) < loop_cfg.minimum_quality_score:
        draft.loop = False
        draft.loop_note = f"루프 미사용: 품질 {q.loop_score} < {loop_cfg.minimum_quality_score} ({q.loop_note})"
    else:
        draft.loop_note = f"루프 사용: 품질 {q.loop_score} ({q.loop_note})"
    return draft
