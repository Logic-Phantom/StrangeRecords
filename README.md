# 기묘한 기록 (StrangeRecords) — AI YouTube Shorts 자동 제작·업로드 시스템

매일 **새로운 주제 선정 → 자료 조사 → 대본 → 음성 → 시각자료 → 자막 → 편집 → 품질 검사 → YouTube 업로드 → 12:00 예약 공개 → 이력 저장** 을 자동으로 수행하는 Python 프로젝트입니다.

- AI 엔진: **Google Gemini API 하나만 사용** (무료 티어 기준, 모델명은 `config.yaml` 한 곳에서 관리)
- 음성: Edge TTS (무료) · 시각자료: Pexels / Pixabay (무료 API) · 자막: faster-whisper (로컬) · 편집: FFmpeg · DB: SQLite
- 출력: **1080x1920 · 9:16 · 30fps · H.264 · AAC · MP4**, 한국어 40~60초

---

## 1. 파이프라인

```text
[1] DB 기존 주제 조회 → [2] Gemini 주제 후보 → [3] 중복 검사(키워드 + 텍스트 유사도 + Gemini 의미 판단)
→ [4] 최종 주제 → [5] 자료 조사(Wikipedia + Gemini 사실/추측 구분) → [6] Gemini 대본(Hook 포함)
→ [7] 대본 검증(길이/금지표현/사실여부 고지) → [8] Gemini Scene 분할 + 영어 검색어
→ [9] Edge TTS (Scene 별 합성 → 실제 길이 확정, 길면 속도 자동 조정)
→ [10] Pexels → Pixabay → 로컬 → (AI 이미지) → 자체 그래픽 순으로 자료 수집
→ [11] faster-whisper 단어 타이밍 + 대본 텍스트 정렬 → Shorts 자막
→ [12] FFmpeg: 9:16 crop / Ken Burns / 전환 / 자막 / BGM 덕킹 / 효과음 / -14 LUFS
→ [13] 품질 검사(실패 시 업로드 금지) → [14] Gemini 제목·설명·해시태그 (+ 출처 자동 표기)
→ [15] YouTube 업로드 → [16] 12:00 예약 공개 → [17] SQLite + JSON 이력 저장
```

음성을 시각자료보다 먼저 만드는 이유: Scene 별 실제 음성 길이를 알아야 영상 클립 길이를 정확히 고를 수 있기 때문입니다.

모든 단계 결과는 `jobs.state_json` 과 `data/`, `output/` 에 저장되므로, 실패하면 `retry` 로 **실패한 단계부터** 다시 실행합니다.

## 2. 설치

### Windows (운영 환경 권장)

1. [Python 3.11+](https://www.python.org/downloads/) 설치 (설치 화면에서 *Add python.exe to PATH* 체크)
2. 프로젝트 폴더에서 `setup_windows.bat` 실행 → 가상환경, 패키지, DB, 기본 BGM/효과음 생성
3. `.env` 에 API Key 입력 (아래 3절)
4. `run_test.bat --offline` → API Key 없이 샘플 대본으로 영상 제작 확인
5. `run_test.bat` → Gemini 로 실제 주제/대본 제작 확인

### macOS / Linux

```bash
bash scripts/setup_macos.sh
.venv/bin/python -m app.main test --offline
```

> **FFmpeg**: 별도 설치가 필요 없습니다. PATH 에 `ffmpeg` 가 없으면 `imageio-ffmpeg` 패키지에 포함된 FFmpeg(libx264/AAC 포함)를 자동 사용합니다. ffprobe 가 없으면 PyAV 로 영상을 분석합니다.
>
> **Whisper 모델**: 첫 실행 시 `models/` 에 faster-whisper `small` 모델(약 460MB)을 내려받습니다.

## 3. API Key (`.env`)

| 항목 | 필수 | 비용 | 발급 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | 무료 티어 (한도/모델은 수시 변경) | https://aistudio.google.com/apikey |
| `PEXELS_API_KEY` | 권장 | 무료 | https://www.pexels.com/api/ |
| `PIXABAY_API_KEY` | 권장 | 무료 | https://pixabay.com/api/docs/ |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | 업로드 시 | 무료 (일일 할당량 10,000 units) | Google Cloud Console → YouTube Data API v3 사용 설정 → OAuth 클라이언트 ID(**데스크톱 앱**) |
| Edge TTS | - | 무료, Key 불필요 | - |
| Wikipedia | - | 무료, Key 불필요 | - |

- Pexels/Pixabay Key 가 없으면 `assets/images`, `assets/videos` 의 로컬 자료 → 자체 제작 그래픽(Pillow)으로 대체되어 **제작은 멈추지 않습니다**.
- Gemini 무료 모델/한도가 바뀌면 `python -m app.main models` 로 사용 가능한 모델을 확인하고 `config.yaml` 의 `gemini.model` (또는 `.env` 의 `GEMINI_MODEL`)만 바꾸면 됩니다.
- 하루 Gemini 호출 수: 주제 1 + 중복 판단 1 + 자료 정리 1 + 대본 1 + Scene 1 + 메타데이터 1 ≈ **6회** (+ 검증 실패 시 재요청).

## 4. 명령어

```bash
python -m app.main today        # 오늘 영상 제작 (production 모드면 업로드 + 12:00 예약 공개)
python -m app.main test         # 영상까지만 제작 (업로드 안 함)
python -m app.main test --offline   # Gemini 없이 샘플 대본(tests/fixtures)으로 제작
python -m app.main topic        # 주제 후보 + 중복 검사 결과 미리보기 (저장 안 함)
python -m app.main history      # 제작 이력
python -m app.main retry [JOB]  # 실패 작업을 실패한 단계부터 재시도
python -m app.main upload [JOB] # 제작 완료(rendered) 영상 업로드
python -m app.main auth         # YouTube 최초 OAuth 인증 (token.json 저장)
python -m app.main setup        # 폴더/DB/기본 BGM·효과음 생성
python -m app.main doctor       # 환경 점검
python -m app.main models       # 사용 가능한 Gemini 모델 목록
python -m app.main schedule     # (보조) Python 스케줄러
```

옵션: `today --force` (오늘 이미 업로드했어도 제작), `today --no-upload`, `--topic "주제"`, `--category unsolved_mystery`

## 5. 운영 모드

`app/config/config.yaml`

```yaml
app:
  mode: "development"   # 주제~품질검사까지 수행, YouTube 업로드 X  →  `upload` 명령으로 나중에 업로드 가능
  # mode: "production"  # 전체 파이프라인 + 업로드
youtube:
  publish_mode: "scheduled"   # private | scheduled | public
  publish_time: "12:00"
  late_policy: "public_now"   # 완성 시점이 12:00 이후면: public_now | next_day | private
```

`today` 는 **하루 1개 정책**을 지킵니다: 오늘 이미 업로드했으면 종료, 오늘 실패한 작업이 있으면 새 주제 대신 이어서 실행, 업로드 대기 영상이 있으면 업로드합니다.

## 6. YouTube 연결

1. Google Cloud Console 에서 프로젝트 생성 → **YouTube Data API v3** 사용 설정
2. OAuth 동의 화면 구성(외부, 테스트 사용자에 본인 계정 추가) → OAuth 클라이언트 ID(**데스크톱 앱**) 생성
3. `.env` 에 `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` 입력 (또는 다운로드한 JSON 을 `credentials.json` 으로 저장)
4. `python -m app.main auth` → 브라우저 로그인/승인 → `token.json` 저장 (이후 자동 갱신)
5. 먼저 `publish_mode: "private"` 로 업로드 테스트 → 확인 후 `scheduled`

> ⚠️ **API 감사(Audit) 전 제한**: 2020-07-28 이후 생성된 **미인증 API 프로젝트**로 업로드한 영상은 YouTube 정책상 **비공개(private)로 잠깁니다**. 예약/공개 업로드를 하려면 [YouTube API 감사 신청](https://support.google.com/youtube/contact/yt_api_form)을 통과해야 합니다.
>
> ⚠️ OAuth 동의 화면이 "테스트" 상태면 refresh token 이 7일 후 만료될 수 있습니다. 장기 운영 시 앱을 "프로덕션"으로 게시하세요.
>
> 업로드 시 `containsSyntheticMedia`(AI 생성 콘텐츠 고지)를 설정하고, 설명란에 AI 활용 사실을 자동으로 표기합니다.

## 7. 자동 실행

### Windows 작업 스케줄러 (기본)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1            # 매일 10:00
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1 -Time 09:30
```

- `run_daily.bat` 은 자기 위치를 프로젝트 루트로 사용하므로 폴더를 옮겨도 동작합니다.
- 10:00 에 PC 가 꺼져 있었다면 켜진 직후 실행(StartWhenAvailable), 절전 상태면 깨워서 실행(WakeToRun)합니다.
- 실행 로그: `logs/task_scheduler.log`, 일자별 상세 로그: `logs/YYYY-MM-DD.log`

### macOS (launchd)

```bash
bash scripts/install_macos_launchd.sh 10 0
```

### 보조: Python 스케줄러

`python -m app.main schedule` — 프로세스가 켜져 있는 동안 매일 `schedule.generate_time` 에 실행합니다.

## 8. 프로젝트 구조

```text
app/
├── main.py                    # CLI
├── schemas.py                 # 콘텐츠 데이터 구조 (Gemini JSON 검증)
├── config/  settings.py, config.yaml
├── ai/      gemini_client.py, topic_generator.py, script_generator.py, scene_generator.py,
│            metadata_generator.py, validator.py, json_parser.py, prompts/*.txt
├── topics/  duplicate_checker.py, selector.py
├── research/researcher.py     # Wikipedia 자료 수집
├── voice/   base.py, edge_tts.py, narration.py
├── assets/  base.py, pexels.py, pixabay.py, local.py, image_generator.py, asset_manager.py,
│            audio_library.py, synth_audio.py
├── subtitle/whisper.py, chunker.py
├── video/   ffmpeg.py, scenes.py, subtitle_renderer.py, renderer.py
├── quality/ checker.py
├── youtube/ auth.py, uploader.py
├── database/db.py, models.py, repositories.py
├── scheduler/scheduler.py
├── pipeline/daily_pipeline.py
└── utils/   logger.py, files.py, retry.py
assets/  music/ (BGM + music.json)  sfx/  images/  videos/  fonts/
data/    database.sqlite  topics/  scripts/  metadata/  sources/
output/  audio/  videos/  subtitles/  thumbnails/  work/
```

- 모든 외부 서비스는 Adapter 구조 (`VoiceProvider`, `AssetProvider`)
- 긴 프롬프트는 코드가 아닌 `app/ai/prompts/*.txt` 에서 관리 (`{{변수}}` 치환)
- 재시도: 1차 실패 → 5~10초 → 2차 → 15~30초 → 3차 → 실패 기록 (무한 재시도 없음). 잘못된 API Key 등은 즉시 실패
- Gemini JSON 파싱/검증 실패: 오류 내용을 피드백으로 붙여 최대 2회 재요청 후 작업 실패 처리

## 9. 콘텐츠 원칙

- 카테고리 5개를 순환(가장 오래 안 쓴 카테고리 우선): 미제 사건 / 기묘한 현상·도시전설 / 희귀·전설의 생물 / 두뇌 자극 / 역사의 은밀한 이야기
- 모든 콘텐츠에 `FACT / UNCONFIRMED / LEGEND / URBAN_LEGEND / FICTION` 표시. FACT 가 아니면 설명란에 고지 문구가 자동으로 들어갑니다.
- 과도한 낚시 표현 금지 목록: `script.banned_phrases`
- 같은 Pexels/Pixabay 자료는 다른 영상에서 재사용하지 않습니다.

## 10. 저작권 / 라이선스

| 자료 | 라이선스 | 비고 |
|---|---|---|
| Pexels | [Pexels License](https://www.pexels.com/license/) | 상업적 사용 무료, 출처 표기 권장 → 설명란 자동 표기 |
| Pixabay | [Pixabay Content License](https://pixabay.com/service/license-summary/) | 상업적 사용 무료, 핫링크 금지 → 다운로드 후 사용 |
| Wikipedia | CC BY-SA | 사실 확인용 참고자료, 설명란에 출처 링크 표기 |
| 기본 BGM/효과음 | CC0 | `synth_audio.py` 로 직접 합성 (외부 음원 아님) |
| Edge TTS 음성 | Microsoft 서비스 약관 | 사용 전 최신 약관 확인 |
| Gemini 생성 텍스트/이미지 | Google 약관 | AI 생성 사실 고지 (`containsSyntheticMedia`, 설명란) |

- 직접 BGM 을 추가할 때는 `assets/music/` 에 파일을 넣고 `music.json` 에 `title, artist, source, license, url, mood` 를 기록하세요. **license 가 없는 음원은 사용하지 않습니다.**
- 로컬 이미지/영상(`assets/images`, `assets/videos`)은 같은 이름의 `.json` 에 `tags, author, license, source_url` 을 적을 수 있습니다.
- 각 서비스의 약관/정책은 변경될 수 있으므로 실제 운영 전에 반드시 최신 정책을 확인하세요.

## 11. 테스트

```bash
.venv/bin/python -m pytest -q        # 단위 + FFmpeg 렌더링 통합 테스트
python -m app.main test --offline    # 실제 TTS/Whisper/FFmpeg 로 59초 샘플 Shorts 제작
```

## 12. 완료 체크리스트

| 항목 | 상태 |
|---|---|
| Gemini 연동 / 대본 / Hook / Scene / 제목 / 설명 / 해시태그 | 구현 + 오프라인 테스트 (실제 호출은 API Key 입력 후 `test` 로 확인) |
| 주제 DB / 주제 중복 방지 | 구현 + 테스트 |
| Edge TTS / Whisper / 자막 / FFmpeg / BGM / 효과음 / 1080x1920 출력 | 구현 + 실제 제작 검증 |
| Pexels / Pixabay / Asset Manager | 구현 (API Key 입력 후 확인 필요, 없으면 fallback 동작 검증됨) |
| 품질 검사 / SQLite 기록 / Retry / Error Log | 구현 + 테스트 |
| YouTube OAuth / Upload / 예약 공개 | 구현 (클라이언트 ID 입력 후 `auth` → private 업로드로 확인 필요) |
| Windows Scheduler / run_daily.bat / README / .env.example | 작성 완료 |
