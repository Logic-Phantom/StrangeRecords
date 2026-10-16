"""합성 음성의 상업적 이용 권리 상태.

edge-tts 라이브러리(오픈소스 코드)의 라이선스는 '코드'를 쓰는 조건일 뿐이고, 그 라이브러리가 접속하는
Microsoft Edge '소리 내어 읽기' 음성 서비스의 출력물을 상업적으로 써도 된다는 허락이 아니다.
이 서비스는 공개 API/약관으로 제공되는 상품이 아니며(브라우저용 비공개 엔드포인트), 수익화 영상에 쓰는 것을
명시적으로 허용하는 공개 약관이나 서면 답변은 확인되지 않았다 → 기본값 unverified.

- 코드가 자동으로 verified 로 바꾸지 않는다. 설정에서 사람이 verified 로 바꾸고 rights_evidence 에 근거를 적어야 인정한다.
- 음성이 합성된다는 사실은 이용 권한의 근거가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import Settings

VALID = ("unverified", "verified", "restricted")


@dataclass
class RightsStatus:
    status: str                       # unverified | verified | restricted
    review_severity: str              # 콘텐츠 검수에서의 처리: warning | critical
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "verified"


def voice_rights(settings: Settings) -> RightsStatus:
    v, review = settings.voice, settings.review
    status = (v.rights_status or "unverified").strip().lower()
    notes: list[str] = []
    if status not in VALID:
        notes.append(f"알 수 없는 rights_status '{v.rights_status}' → unverified 로 처리")
        status = "unverified"
    if status == "verified" and not v.rights_evidence.strip():
        notes.append("verified 로 설정됐지만 rights_evidence(약관 URL·서면 답변 위치)가 없어 unverified 로 처리")
        status = "unverified"
    if v.provider != "edge_tts":
        notes.append(f"voice.provider={v.provider} (이 점검은 Edge TTS 기준)")
    if status == "unverified":
        notes += [
            "edge-tts 는 Edge 브라우저 '소리 내어 읽기'용 비공개 엔드포인트를 사용합니다 (공식 API·상업용 약관 아님).",
            "edge-tts 의 오픈소스 라이선스는 코드 이용 조건이며, Microsoft 음성 출력물의 상업적 이용 허락이 아닙니다.",
            "확인 방법: Microsoft 의 공식 약관/서면 답변으로 수익화 영상 사용이 허용되는지 확인 → voice.rights_evidence 에 근거,"
            " voice.rights_status: verified, voice.rights_checked_at 기록.",
            "대안(이번 작업 범위 밖): 상업 이용 조건이 명시된 유료 TTS(예: Azure AI Speech) 계약.",
        ]
    elif status == "verified":
        notes.append(f"근거: {v.rights_evidence} (확인일 {v.rights_checked_at or '미기록'})")
    else:
        notes.append("restricted: 상업적 이용 불가로 확인됨 → 수익화 영상에 사용하지 마세요.")
    strict = review.strict or review.unverified_rights_policy == "review_needed"
    if status == "restricted":
        severity = "critical"
    elif status == "unverified":
        severity = "critical" if strict else "warning"
    else:
        severity = "ok"
    return RightsStatus(status, severity, notes)
