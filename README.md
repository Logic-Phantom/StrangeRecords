# 기묘한 기록 (StrangeRecords) — AI YouTube Shorts 자동 제작·업로드 시스템

매일 **새로운 주제 선정 → 자료 조사 → 대본 → 음성 → 시각자료 → 자막 → 편집 → 품질 검사 → YouTube 업로드(즉시 공개) → 이력 저장** 을 자동으로 수행하는 Python 프로젝트입니다. (Windows 작업 스케줄러로 매일 22:00 실행)

- AI 엔진: **Google Gemini API 하나만 사용** (기본 `gemini-3.5-flash`, 과부하 시 자동 대체 모델 전환, 모델명은 `config.yaml` 한 곳에서 관리)
- 음성: Edge TTS (무료) · 시각자료: **대본 기반 Gemini AI 장면 이미지** → **Hugging Face SDXL (대체 AI 이미지)** → Pexels / Pixabay (무료 API) · 자막: faster-whisper (로컬) · 편집: FFmpeg · DB: SQLite
- 출력: **1080x1920 · 9:16 · 30fps · H.264 · AAC · MP4**, 한국어 40~60초

---

## 1. 파이프라인

```text
[1] DB 기존 주제 조회 → [2] Gemini 주제 후보 → [3] 중복 검사(키워드 + 텍스트 유사도 + Gemini 의미 판단)
→ [4] 최종 주제 → [5] 자료 조사(Wikipedia + Gemini 사실/추측 구분) → [6] Gemini 대본(Hook 포함)
→ [7] 대본 검증(길이/금지표현/사실여부 고지) → [8] Gemini Scene 분할 + 영어 검색어
→ [9] Edge TTS (Scene 별 합성 → 실제 길이 확정, 길면 속도 자동 조정)
→ [10] AI 장면 이미지(Scene 별 visual_prompt: Gemini → Hugging Face) → Pexels → Pixabay → 로컬 → 자체 그래픽 순으로 자료 수집
→ [11] faster-whisper 단어 타이밍 + 대본 텍스트 정렬 → Shorts 자막
→ [12] FFmpeg: 9:16 crop / Ken Burns / 전환 / 자막 / BGM 덕킹 / 효과음 / -14 LUFS
→ [13] 품질 검사(실패 시 업로드 금지) → [14] Gemini 제목·설명·해시태그 (+ 출처 자동 표기)
→ [15] YouTube 업로드 → [16] 공개(즉시 또는 12:00 예약) → [17] SQLite + JSON 이력 저장
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
| `HF_API_KEY` | 권장 | 무료 월 크레딧 | https://huggingface.co/settings/tokens → Fine-grained 토큰, **"Make calls to Inference Providers"** 권한 체크 |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | 업로드 시 | 무료 (일일 할당량 10,000 units) | Google Cloud Console → YouTube Data API v3 사용 설정 → OAuth 클라이언트 ID(**데스크톱 앱**) |
| Edge TTS | - | 무료, Key 불필요 | - |
| Wikipedia | - | 무료, Key 불필요 | - |

- 시각자료는 **Gemini 이미지 모델이 Scene 마다 대본 내용대로 그린 이미지**를 먼저 사용합니다 (`assets.ai_image`). 같은 `GEMINI_API_KEY` 를 쓰지만, 무료 티어에서 이미지 모델이 막혀 있으면 [Google AI Studio](https://aistudio.google.com/) 에서 **결제(Billing) 연결**이 필요합니다 (장당 약 $0.04, 영상 1개 6~9장).
- Gemini 이미지가 실패(무료 티어 `limit: 0`, 429 RESOURCE_EXHAUSTED, 권한 오류)하면 **같은 Scene 에서 바로 Hugging Face Inference API** (`assets.hf_image`, 기본 `stabilityai/stable-diffusion-xl-base-1.0`, 768x1344)로 같은 프롬프트를 그립니다. `provider: "auto"` 라 그 모델을 서비스 중인 Inference Provider(현재 SDXL 은 fal-ai)로 자동 라우팅되며, 토큰 권한 없음(403)·크레딧 소진(402)이면 이번 작업에서 제외하고 Pexels 로 넘어갑니다.
- AI 이미지가 모두 실패하면 Pexels/Pixabay → `assets/images`, `assets/videos` 의 로컬 자료 → 자체 제작 그래픽(Pillow, 어두운 배경)으로 대체되어 **제작은 멈추지 않습니다**. 영상이 어두운 배경만 나오면 로그에서 `ai_image 자료 수집 실패` 원인을 확인하세요.
- Gemini 무료 모델/한도가 바뀌면 `python -m app.main models` 로 사용 가능한 모델을 확인하고 `config.yaml` 의 `gemini.model` (또는 `.env` 의 `GEMINI_MODEL`)만 바꾸면 됩니다.
- 기본 모델이 과부하(503)/한도 초과(429)로 3회 실패하면 `gemini.fallback_models` 순서(`gemini-flash-latest` → `gemini-2.5-flash` → `gemini-flash-lite-latest`)로 자동 전환하고, 그 실행 동안은 전환된 모델을 유지합니다.
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
python -m app.main upload --privacy private   # 이번 업로드만 비공개로 (scheduled | public 도 가능)
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
  mode: "production"    # 전체 파이프라인 + 업로드 (매일 자동 실행에 필요)
  # mode: "development" # 주제~품질검사까지 수행, YouTube 업로드 X  →  `upload` 명령으로 나중에 업로드 가능
youtube:
  publish_mode: "public"      # public(업로드 즉시 공개) | scheduled(publish_time 예약 공개) | private
  publish_time: "12:00"
  late_policy: "public_now"   # 완성 시점이 12:00 이후면: public_now | next_day | private
```

`today` 는 **하루 1개 정책**을 지킵니다: 오늘 이미 업로드했으면 종료, 오늘 실패한 작업이 있으면 새 주제 대신 이어서 실행, 업로드 대기 영상이 있으면 업로드합니다.

## 6. YouTube 연결

1. Google Cloud Console 에서 프로젝트 생성 → **YouTube Data API v3** 사용 설정
2. OAuth 동의 화면 구성(외부, 테스트 사용자에 본인 계정 추가) → OAuth 클라이언트 ID(**데스크톱 앱**) 생성
3. `.env` 에 `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` 입력 (또는 다운로드한 JSON 을 `credentials.json` 으로 저장)
4. `python -m app.main auth` → 브라우저 로그인/승인 → `token.json` 저장 (이후 자동 갱신)
5. 먼저 `python -m app.main upload --privacy private` 로 비공개 업로드 테스트 → 확인 후 `scheduled`

**로그인 오류 해결**

| 화면 메시지 | 원인 / 해결 |
|---|---|
| `액세스 차단됨: (앱)은(는) Google 인증 절차를 완료하지 않았습니다` | OAuth 동의 화면이 "테스트" 상태인데 로그인 계정이 테스트 사용자가 아님 → [Google 인증 플랫폼 → 대상](https://console.cloud.google.com/auth/audience) → **테스트 사용자 추가** 후 `auth` 다시 실행 |
| `Google에서 확인하지 않은 앱` | 테스트 상태에서는 정상 → **고급 → (앱)(으)로 이동** |
| `redirect_uri_mismatch` | OAuth 클라이언트가 "웹 애플리케이션" 유형 → **데스크톱 앱** 유형으로 다시 생성 |
| `YouTube Data API v3 has not been used` | [API 라이브러리](https://console.cloud.google.com/apis/library/youtube.googleapis.com)에서 사용 설정 |

> ⚠️ **API 감사(Audit) 전 제한**: 2020-07-28 이후 생성된 **미인증 API 프로젝트**로 업로드한 영상은 YouTube 정책상 **비공개(private)로 잠깁니다**. 예약/공개 업로드를 하려면 [YouTube API 감사 신청](https://support.google.com/youtube/contact/yt_api_form)을 통과해야 합니다.
>
> ⚠️ OAuth 동의 화면이 "테스트" 상태면 refresh token 이 7일 후 만료될 수 있습니다. 장기 운영 시 앱을 "프로덕션"으로 게시하세요.
>
> 업로드 시 `containsSyntheticMedia`(AI 생성 콘텐츠 고지)를 설정하고, 설명란에 AI 활용 사실을 자동으로 표기합니다.

## 7. 자동 실행

### GitHub Actions (기본, PC 꺼져 있어도 동작)

`.github/workflows/daily-shorts.yml` 이 매일 **22:07 KST** (GitHub 혼잡 시 수십 분 지연 가능) GitHub 서버(Ubuntu)에서 `today` 를 실행해 제작 → 업로드합니다.

1. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에 등록
   | Secret | 값 |
   |---|---|
   | `GEMINI_API_KEY` | Gemini API Key (필수) |
   | `YOUTUBE_TOKEN_JSON` | 로컬 `token.json` 파일 내용 전체 (필수, `Get-Content token.json -Raw \| Set-Clipboard` 로 복사) |
   | `HF_API_KEY` | Hugging Face 토큰 (권장) |
   | `PEXELS_API_KEY` / `PIXABAY_API_KEY` | 선택 |
2. **Actions → Daily Shorts → Run workflow** 로 수동 실행해 확인 (`test` = 제작만, `today` = 제작 + 업로드)
- 주제 이력/하루 1개 정책 DB(`data/database.sqlite`)는 실행마다 `pipeline-state` 브랜치에 저장되어 다음 실행이 이어 씁니다.
- 실패하면 같은 서버에서 5분/10분 뒤 실패한 단계부터 최대 3회 실행합니다. 완성 영상은 Actions 실행 화면의 Artifacts 에 3일 보관됩니다.
- ⚠️ OAuth 동의 화면이 "테스트" 상태면 refresh token 이 7일 뒤 만료되어 업로드가 멈춥니다 → [Google 인증 플랫폼 → 대상](https://console.cloud.google.com/auth/audience) 에서 **앱 게시(프로덕션)** 후 `auth` 를 다시 실행하고 `YOUTUBE_TOKEN_JSON` 을 갱신하세요.
- 공개 저장소는 60일 동안 활동이 없으면 예약 워크플로가 비활성화될 수 있습니다 (Actions 화면에서 다시 Enable).
- 클라우드와 Windows 작업 스케줄러를 동시에 쓰면 DB 가 달라 하루 2개가 올라가므로 하나만 사용합니다.

### Windows 작업 스케줄러 (PC 에서 실행할 때)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1                       # 매일 22:00 (+23:00 재시도)
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1 -Time 21:30 -RetryTime ""
```

- `run_daily.bat` 은 자기 위치를 프로젝트 루트로 사용하므로 폴더를 옮겨도 동작합니다.
- 22:00 에 PC 가 꺼져 있었다면 켜진 직후 실행(StartWhenAvailable), 절전 상태면 깨워서 실행(WakeToRun), 네트워크 연결 후 실행(RunOnlyIfNetworkAvailable)합니다. `run_daily.bat` 도 DNS 가 붙을 때까지 최대 10분 기다립니다.
- 23:00 재시도 실행은 하루 1개 정책 덕분에 이미 업로드했으면 바로 끝나고, 실패했으면 실패 단계부터 이어서 실행합니다.
- PC 가 켜져 있고 Windows 에 로그인된 상태여야 실행됩니다.
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
| Gemini API 연동 / 대본 / Hook / Scene / 제목 / 설명 / 해시태그 | ✅ 실제 Gemini 호출로 제작 확인 |
| 주제 DB / 주제 중복 방지 | ✅ 구현 + 테스트 (실제 후보 8개 중복 검사 통과) |
| Edge TTS / Whisper / 자막 / FFmpeg / BGM / 효과음 / 1080x1920 출력 | ✅ 실제 제작 검증 |
| 품질 검사 / SQLite 기록 / Retry / Error Log | ✅ 실제 실패 → `retry` 로 재개 검증 |
| YouTube OAuth / Upload | ✅ 비공개 업로드 성공 |
| YouTube 자동 공개 (`publish_mode: public`) | ⏳ 구현 완료, API 감사 통과 후 확인 필요 (미인증 프로젝트는 비공개로 잠김, 업로드 직후 상태 확인해 경고) |
| AI 장면 이미지 (Gemini) | ⏳ 구현 + 테스트 완료, Gemini 결제 연결 후 확인 필요 (무료 티어는 이미지 모델 한도 0) |
| AI 장면 이미지 대체 (Hugging Face SDXL) | ⏳ 구현 + 테스트 완료, `HF_API_KEY` 등록됨 → 토큰에 Inference Providers 권한 추가 후 확인 필요 (현재 403) |
| Pexels / Pixabay | ⏳ 구현 완료, API Key 입력 후 확인 필요 (현재는 자체 그래픽으로 대체) |
| Windows Scheduler / run_daily.bat | ✅ Windows PC 에 등록 (매일 22:00 + 23:00 재시도), `token.json` 준비 후 업로드 동작 |
| Windows 실제 제작 | ✅ Windows 11 / Python 3.14 에서 `today` 로 영상 제작·품질 검사 통과 (2026-10-08) |
| README / .env.example | ✅ |

## 13. 작업 내역

### 2026-10-05 — 1차 구축 (`32d5be9`)

**환경 분석**
- macOS 15.6 (arm64), 기존 Python 3.9 / FFmpeg 없음 / 빈 프로젝트 폴더
- `uv` 로 Python 3.11 설치 → `.venv` 생성, `requirements.txt` 설치
- FFmpeg: 시스템 설치 대신 `imageio-ffmpeg` 내장 FFmpeg 7.1 사용 (libx264, AAC, zoompan, xfade 지원 확인), ffprobe 대신 PyAV 로 분석

**구현 (마스터 프롬프트 Phase 1~11)**
- Phase 1: 폴더 구조, `config.yaml` + `.env` 설정, SQLite(topics/jobs/assets/step_logs), 로그, CLI
- Phase 2: `GeminiClient` (JSON 모드, 파싱 실패 시 피드백 붙여 최대 2회 재요청), 주제/대본/Scene/메타데이터 생성기, 프롬프트 파일 분리
- Phase 3: 중복 검사 = 키워드(조사 제거·동의어 정규화) + 문자 bigram 유사도 + Gemini 의미 판단
- 자료 조사: Wikipedia API(ko/en) → Gemini 가 FACT/UNCONFIRMED/LEGEND 등으로 정리
- Phase 4: Edge TTS 를 Scene 별로 합성 → 실제 Scene 시간 확정, 길이 초과 시 말하기 속도 자동 상향
- Phase 5: Pexels → Pixabay → 로컬 → (Gemini 이미지) → 자체 그래픽 순 Asset Manager, 출처/라이선스 기록, 다른 영상에서 쓴 자료 재사용 방지
- Phase 6: faster-whisper 단어 타이밍 + 대본 텍스트 정렬로 오탈자 없는 Shorts 자막
- Phase 7: Scene 클립(9:16 crop, Ken Burns) → xfade 전환 → Pillow 자막(강조 단어 색상) + BGM 덕킹 + 효과음 + -14 LUFS
- 저작권 없는 기본 BGM 3곡 / 효과음 6종을 FFmpeg 로 직접 합성 (CC0)
- Phase 8: 품질 검사 (길이, 해상도, fps, 코덱, 오디오, 자막, 파일 크기, 검은 화면) 실패 시 업로드 금지
- Phase 9: YouTube OAuth / 업로드 / 12:00 예약 공개(늦으면 late_policy)
- Phase 10~11: `run_daily.bat`, 작업 스케줄러 등록 PowerShell, macOS launchd, 하루 1개 정책, 실패 단계부터 재개

**테스트 중 발견해 수정한 문제**
- 대본 첫 문장을 Whisper `initial_prompt` 로 주면 그 문장을 건너뛰어 첫 자막이 6초에 나옴 → 제거, Scene 경계 겹침 수정, 인식 누락 시 음성 길이 비율 배분
- 실측 TTS 속도 4.9자/초 (예상 6.8자/초) → `chars_per_second` 보정
- 한 글자 강조어("한")가 "한가운데서"에도 칠해짐 → 정확히 일치할 때만 강조
- 출처 표기 중복 줄 제거

### 2026-10-05 — Gemini 실제 연동 (`ae06ff2`)

- Gemini API Key 등록 후 실제 제작: 주제 "디아틀로프 고개의 비극" 선정 → 위키백과 4건 조사 → 237자 대본 → 8개 Scene → 49.8초 영상, 품질 검사 통과
- `gemini-2.5-flash` 503(과부하) 반복 → `fallback_models` 자동 전환 기능 추가
- Gemini 가 한국어 글자 수를 잘 못 맞춤(389자 → 199자 → 341자로 3회 실패) → 문장 수(8~11문장, 문장당 20~30자)로 지시, 줄이거나 늘릴 분량을 구체적으로 피드백, 허용 범위를 목표의 0.85~1.2배로 완화
- 실패한 작업을 `retry` 로 대본 단계부터 재개해 완성 (주제/자료 조사는 저장된 결과 재사용)

### 2026-10-05 — YouTube 연동 (`2095145`)

- 기본 Gemini 모델 `gemini-3.5-flash` 로 변경 (호출 확인), 대체 모델 3개 설정
- YouTube OAuth 클라이언트 등록 → "액세스 차단됨" 오류는 테스트 사용자 추가로 해결 → 인증 완료, `token.json` 저장
- `upload --privacy` 옵션 추가 → 디아틀로프 고개 영상 **비공개 업로드 성공** (https://youtube.com/shorts/cdllfZBnxa0), API 로 상태 확인
- 자동 테스트 36개 통과

### 2026-10-08 — AI 장면 이미지 / 자동 공개 / 매일 자동 실행 수정 (`af884bb`)

**Windows PC 환경 구성**
- Windows 11 Home, Python 3.14.8 (3.11 미설치 → 3.14 로 `.venv` 생성, `requirements-dev.txt` 설치 정상)
- FFmpeg: `imageio-ffmpeg` 내장 7.1 사용, 한글 폰트: `C:/Windows/Fonts/malgunbd.ttf` 자동 탐색
- `setup` 실행 → `.env` 생성(빈 템플릿), DB 생성, 기본 BGM 3곡 확인
- `GEMINI_API_KEY` 는 Windows 사용자 환경변수에 이미 설정되어 있어 `.env` 없이도 동작 (`load_dotenv(override=False)`)
- `doctor` 결과: YouTube client / token 없음 → 업로드 전 `auth` 필요

**문제와 원인**
- 영상 배경이 어두운 단색만 나옴 → Pexels/Pixabay Key 없음 + `ai_image.enabled: false` 라서 매번 마지막 fallback(자체 그래픽 그라디언트)이 사용됨
- 업로드 영상이 비공개 → 테스트 업로드를 `--privacy private` 로 했고, 미인증(감사 전) API 프로젝트는 YouTube 가 비공개로 잠금
- 하루 1개 자동 실행이 안 됨 → `app.mode: "development"` 라 실행돼도 업로드를 건너뜀, Windows PC 에 작업 스케줄러/`.venv`/`.env`/`token.json` 이 없었음

**수정**
- AI 장면 이미지: Scene 의 `visual_prompt`(대본 기반) + 영상 제목/주제 + 공통 화풍(`ai_image.style`) + 분위기로 Gemini 이미지 모델이 9:16 장면 이미지를 생성, provider 순서 1순위로 변경
  - 이미지 모델 fallback(`ai_image.fallback_models`), 한도/권한 오류 시 즉시 다음 provider 로 전환(Scene 마다 재시도하지 않음)
  - 무료 티어 한도 0(`limit: 0`)은 재시도 없이 "결제 연결 필요" 안내 후 Pexels → … → 자체 그래픽으로 계속 제작
  - Scene 프롬프트에 나레이션 내용을 구체적으로 그리고 장면 간 시대/장소가 이어지도록 지시 추가
- `publish_mode: "public"` (업로드 즉시 공개), 업로드 직후 실제 공개 상태를 조회해 비공개로 잠기면 감사 신청 안내 경고
- `app.mode: "production"`, Windows 작업 스케줄러 등록(매일 10:00), 등록 스크립트가 `.venv`/`.env`/`token.json`/production 모드 누락을 경고
- 무료 이미지 서비스(Pollinations)는 2026-10 현재 결제 필요(402)로 확인되어 사용하지 않음
- 자동 테스트 40개 통과 (Python 3.14.8, Windows 11)

**실제 확인 결과**
- `models` 로 확인한 이미지 모델: `gemini-3.1-flash-image`, `gemini-3.1-flash-lite-image`, `gemini-2.5-flash-image`, `gemini-3-pro-image(-preview)`, `gemini-3.1-flash-image-preview`
  → 기본 `gemini-3.1-flash-image`, 대체 `gemini-2.5-flash-image` 로 설정
- 현재 API Key 로 이미지 모델 3종 호출 → 모두 `429 RESOURCE_EXHAUSTED ... free_tier ... limit: 0` (무료 티어에서는 이미지 생성 불가, 결제 연결 필요)
  → 이 경우 0.6초 만에 안내 후 대체 자료로 넘어가도록 처리
- Windows 작업 스케줄러 `StrangeRecords Daily Shorts` 등록 완료 (다음 실행 2026-10-09 10:00)

**영상 제작 (`today --no-upload`, job `20261008-173014`)**
- 주제: 미제 사건 카테고리 → "하늘에서 2억과 함께 증발한 남자, DB 쿠퍼 미스터리 🪂" (FACT)
- 단계별 시간: 주제 21초 → 조사 18초 → 대본 10초 → Scene 19초 → 음성 7초 → 자료 2초 → 자막 120초(Whisper 모델 첫 다운로드 포함) → 렌더 90초 → 품질 검사 통과 → 메타데이터 8초, 총 296초
- 결과: `output/videos/20261008-173014.mp4` (14.7MB), 상태 `rendered` (YouTube 인증 전이라 업로드 대기)
- 시각자료: AI 이미지가 무료 티어 한도 0 으로 첫 Scene 에서 제외 → Pexels Key 도 없어 자체 그래픽(어두운 그라디언트) 배경 사용
- 이 PC 의 DB 는 새로 만들어져 기존 주제 0개 → macOS 에서 만든 영상(디아틀로프 고개 등)과의 중복 검사가 되지 않음

### 2026-10-10 — Hugging Face 대체 AI 이미지 / 매일 22:00 자동 실행

**Hugging Face 대체 이미지 (`HuggingFaceImageProvider`)**
- provider 순서 `ai_image → hf_image → pexels → pixabay → local → procedural`. Gemini 가 NonRetryableError 로 제외되면 AssetManager 가 같은 Scene 에서 곧바로 `hf_image` 를 호출
- Gemini 이미지 429 는 재시도 대기 없이 다음 모델 → 모두 막히면 다음 provider 로 (기존: 5~10초 대기 후 재시도)
- `huggingface_hub.InferenceClient(provider="auto", api_key=HF_API_KEY)` 의 `text_to_image` 로 SDXL 768x1344 생성 (요청 헤더 `Authorization: Bearer`)
- 확인 결과: 예전 `api-inference.huggingface.co` 는 DNS 가 없어졌고, SDXL 은 `hf-inference` 에서 내려가 현재 **fal-ai** provider 로만 서비스 → 직접 URL 대신 `provider="auto"` 라우팅 사용
- 등록한 토큰은 유효(whoami 200)하지만 `403 ... does not have sufficient permissions to call Inference Providers` → 토큰 권한 추가 필요. 이 경우 0.5초 만에 제외하고 다음 provider 로 진행되는 것 확인
- offline 테스트(`test --offline`)와 pytest 는 HF 를 호출하지 않음, 자동 테스트 44개 통과

**매일 자동 실행 22:00**
- 10/10 오전 실행 실패 원인: 절전 해제 직후 DNS 미연결(`getaddrinfo failed`)로 Gemini 3회 실패 → `run_daily.bat` 에 네트워크 대기(최대 10분), 작업에 `RunOnlyIfNetworkAvailable` 추가
- 작업 스케줄러 `StrangeRecords Daily Shorts` 를 매일 22:00 + 23:00(재시도)로 다시 등록, `schedule.generate_time: "22:00"`

**YouTube 인증 + 클라우드 자동 실행**
- Windows PC `.env` 에 OAuth 클라이언트 등록 → `auth` 성공 (채널: 팬텀로즥), `token.json` 저장
- PC 를 켜지 않아도 되도록 GitHub Actions 워크플로 추가 (매일 22:07 KST), DB 는 `pipeline-state` 브랜치로 이어 씀 → 중복 업로드 방지를 위해 Windows 작업 스케줄러 작업은 비활성화

### 남은 작업

1. https://huggingface.co/settings/tokens 에서 등록한 토큰을 Edit → **"Make calls to Inference Providers"** 체크 → Hugging Face 대체 이미지 동작 (`.env` 수정 불필요)
2. Google AI Studio 에서 Gemini API 프로젝트에 결제 연결 → AI 장면 이미지 확인 (안 하면 HF/Pexels/자체 그래픽으로 대체)
3. Pexels(및 Pixabay) API Key 등록 → AI 이미지 실패 시 실제 스톡 영상으로 대체
4. **GitHub 저장소 Secrets 에 `GEMINI_API_KEY`, `YOUTUBE_TOKEN_JSON`, `HF_API_KEY` 등록 → Actions 에서 수동 실행으로 확인 (등록 전에는 클라우드 자동 실행이 실패)**
5. YouTube API 감사 신청 → 통과해야 공개/예약 공개가 실제로 적용됨
6. OAuth 동의 화면을 "프로덕션"으로 게시 (테스트 상태는 토큰 7일 만료 → 자동 업로드가 1주 뒤 멈춤)
7. 인증 후 `.venv\Scripts\python.exe -m app.main upload` 로 대기 중인 DB 쿠퍼 영상 업로드
8. macOS 의 `data/database.sqlite` 를 이 PC 로 복사 → 기존 주제 중복 방지 유지
