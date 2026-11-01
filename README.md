# Personal Gallery

GitHub Pages에서 제공하는 개인 사진 갤러리입니다.

- [갤러리](https://krifle.github.io/gallery/)
- `2026-02/`: 기존 사진 앨범
- `2026-10/`: 공유 키로 여는 암호화 앨범

## 준비

Python 3.11 이상(macOS/Linux), FFmpeg/ffprobe가 필요합니다. FFmpeg에 `libwebp`, `libsvtav1`(또는 `libaom-av1`), AAC 인코더가 있어야 합니다. 웹페이지는 빌드 도구나 외부 CDN 없이 HTML/CSS/JavaScript로 동작합니다.

```sh
python3 -m venv .prepare/venv
.prepare/venv/bin/python -m pip install -r script/requirements.txt
git config core.hooksPath .githooks
```

`core.hooksPath`는 이 저장소에만 설정합니다. 다른 훅이 있는 환경에서는 기존 훅에 `python3 script/check_publish.py --all-staged`를 연결하세요. 훅은 커밋될 **실제 index 내용**을 검사하며, 키나 Python 암호화 패키지 없이도 작동합니다. 훅은 우회될 수 있으므로 공개 전 복호화 검사도 실행합니다.

## 원본과 설명 준비

새 앨범은 저장소 루트에서 다음 명령으로 준비합니다. `index.html`, `.prepare/manifest.json`, 기본 `config.json`과 `.prepare/media/`, `data/`, `media/` 폴더를 생성합니다. 기존 파일은 덮어쓰지 않으므로 기존 앨범에도 실행할 수 있습니다.

```sh
python3 script/template.py 2026-11-01 --title "가을 나들이"
# 월 단위 폴더나 임의의 이름도 사용 가능
python3 script/template.py autumn --date 2026-11-01 --title "가을 나들이"
```

`--date`를 생략하면 날짜 폴더명은 해당 날짜, `YYYY-MM` 폴더명은 해당 월 1일을 사용합니다. 생성 후 `.prepare/media/`에 원본을 넣고 manifest의 `media` 배열에 `{"file": "파일명.jpg"}` 형식으로 등록하세요. Python 가상환경은 위 준비 단계에서 만든 저장소 공용 `.prepare/venv`를 사용합니다.

```text
2026-10/
├── index.html
├── .prepare/             # 전체 Git 제외
│   ├── manifest.json
│   ├── config.json
│   └── media/            # 원본 사진·영상
├── data/manifest.enc     # 공개할 암호화된 목록
└── media/*.enc           # 공개할 사진·썸네일·영상/음성 조각
```

`.prepare/manifest.json`은 **주석 없는 표준 JSON**으로 작성합니다. 날짜별 `date`, `media`, 미디어별 `file`은 필수입니다. `title`, `description`, `EXIF`는 선택 문자열입니다. 파일명은 `.prepare/media/` 기준 상대경로입니다.

```json
{
  "title": "여행 기록",
  "days": [
    {
      "date": "2026-10-03",
      "title": "첫째 날",
      "description": "첫 문단입니다.\n다음 문단입니다.",
      "media": [
        {"file": "20261003_090000.jpg", "title": "출발"},
        {"file": "20261003_100000.mp4", "description": "여행의 한 장면"}
      ]
    }
  ]
}
```

날짜는 오름차순, 각 그룹 안에서는 사진·영상 구분 없이 파일명 순으로 표시합니다. 같은 날짜를 여러 이벤트로 나눌 수 있으며, 같은 날짜의 그룹은 manifest에 작성한 순서대로 표시합니다. 그룹별 `title`로 이벤트를 구분할 수 있고, “N일의 기록”은 고유 날짜 수로 계산합니다. 파일명 날짜와 그룹 날짜가 달라도 그룹을 자동으로 옮기지 않습니다. 누락 파일, 미등록 파일, 중복 참조, 잘못된 필드, 바깥으로 나가는 경로는 오류로 처리합니다.

## 변환과 암호화

`encrypt.py`와 `preview.py`는 앨범의 `index.html`이 없으면 공통 템플릿에서 자동 생성합니다. 기존 HTML은 유지합니다. 평문 미리보기는 `.prepare/preview.html`에 생성됩니다.

```sh
# JSON·파일 대응·도구 검사
.prepare/venv/bin/python script/encrypt.py 2026-10 --check

# 기본 config 생성 (기존 파일은 덮어쓰지 않음)
.prepare/venv/bin/python script/encrypt.py 2026-10 --write-default-config

# 처음 만들 때: 키를 저장소 밖에 생성하면서 변환·암호화
.prepare/venv/bin/python script/encrypt.py 2026-10 --new-key --key-output ../gallery-keys/2026-10.key

# 기존 키로 다시 만들기 (설정 프롬프트 표시)
.prepare/venv/bin/python script/encrypt.py 2026-10 --key-file ../gallery-keys/2026-10.key

# 저장한 config를 사용해 프롬프트 없이 다시 만들기
.prepare/venv/bin/python script/encrypt.py 2026-10 --non-interactive --key-file ../gallery-keys/2026-10.key
```

키를 지정하지 않으면 화면에 표시하지 않는 숨김 입력으로 받습니다. `--key`도 지원하지만 셸 기록에 남을 수 있으므로 `--key-file`을 권장합니다. 키는 32바이트 난수의 43자 Base64URL이며 일반 비밀번호와 다릅니다. 새 키 파일은 권한 600으로 만들고 기존 파일을 덮어쓰지 않습니다. 키를 분실하면 암호문을 열 수 없으므로 저장소 밖에서 별도 백업하세요.

설정은 `2026-10/.prepare/config.json`에 저장합니다. 주요 기본값:

| 설정 | 기본값 |
| --- | --- |
| 본문 사진 | WebP 품질 30, 긴 변 2000px |
| 썸네일·영상 포스터 | WebP 품질 45, 긴 변 480px |
| 흰 여백 | 0%, 사진에만 적용 |
| 영상 | AV1 1200kbps, 긴 변 1280px, 최대 30fps |
| 음성 | 원본에 있으면 AAC 96kbps, `audio: false`로 제거 가능 |
| 분할 | 약 4초, 영상과 음성 별도 fMP4 트랙 |
| 포스터 시점 | 1초 또는 영상 길이 절반 중 작은 값 |

크기를 줄일 때 비율을 유지하고 확대하지 않습니다. 영상은 짝수 크기로 조절합니다. `video_size_mode: "box"`이면 `video_width`, `video_height` 안에 맞춥니다. HLG/PQ HDR 원본은 FFmpeg의 `zscale`·`tonemap` 필터로 Hable 톤 매핑을 적용해 SDR(BT.709)로 자동 변환하며, 썸네일에도 같은 처리를 적용합니다. HDR 변환에는 `zscale`·`tonemap`·`sidedata` 필터가 포함된 FFmpeg가 필요합니다. 원본 파일은 유지합니다. `libaom-av1`을 선택할 때 `av1_preset`은 0~8, `libsvtav1`은 0~13입니다.

원본은 덮어쓰지 않습니다. 변환 결과·캐시·보고서는 모두 `.prepare` 안에 생깁니다. 메타데이터만 변경하면 기존 미디어 암호문을 재사용합니다. 전체 암호문이 **100,000,000 bytes**를 초과하면 공개 결과를 교체하지 않습니다. 이 한도는 다른 앨범이나 Git 이력 크기와 별도입니다.

## 미리보기

```sh
# 변환된 평문으로 UI·화질 확인 (저장된 설정 재사용)
.prepare/venv/bin/python script/preview.py 2026-10

# 실제 공개용 암호문·키 입력·영상 복호화 확인
.prepare/venv/bin/python script/preview.py 2026-10 --encrypted --port 8001
```

표시된 localhost 주소로 접속하세요. 기본 모드는 `.prepare/preview.html`을 생성하고 필요한 변환을 수행합니다. `--encrypted`는 공개용 파일을 사용하며 브라우저에서 키를 입력합니다. 서버는 해당 앨범과 필요한 화면 파일만 제공하며 원본, 관리 manifest, `.git`, 키 파일은 제공하지 않습니다. 기존 다른 앨범은 이 미리보기 서버의 제공 범위에 포함되지 않습니다. 종료는 Ctrl+C입니다.

## 공개 전 검사와 공유

```sh
# 실제 공개 폴더 전체의 GCM 인증·참조·용량 검사
.prepare/venv/bin/python script/check_publish.py 2026-10 --working-tree --key-file ../gallery-keys/2026-10.key

# 커밋할 파일을 선택적으로 stage한 후 Git index 검사
python3 script/check_publish.py --all-staged

# 별도 배포 artifact를 사용할 때
python3 script/check_publish.py 2026-10 --artifact /path/to/site
```

키 없는 검사는 경로, 헤더와 크기를 확인하며 암호화의 진위까지 보장하지 않습니다. 로컬 빌드 보고서가 있으면 `--working-tree`에서 참조와 해시도 확인합니다. 키가 있는 검사는 manifest 및 모든 리소스의 GCM 인증까지 확인합니다.

`.docs/`, 모든 `.prepare/`, 키와 원본은 Git에 올리지 않습니다. 기존 추적 파일은 `.gitignore`만으로 제외되지 않습니다. 새 앨범에서는 `index.html`, `data/manifest.enc`, `media/*.enc`만 공개합니다. 관리 스크립트·테스트는 Git에 보관해도 되지만 `_config.yml`로 Pages 사이트에서는 제외합니다.

현재 Pages는 **main 브랜치 루트**를 배포합니다. commit/push를 하면 공개 사이트 갱신으로 이어질 수 있습니다. 도구 자체는 commit/push를 실행하지 않습니다. 기존 `2026-02`의 로컬 변경을 함께 올릴지 별도로 검토하고, 새 앨범의 manifest와 리소스는 한 커밋에 반영하세요.

공유 링크 형식:

```text
https://krifle.github.io/gallery/2026-10/#key=<키 파일의 43자 값>
```

키는 `?key=`가 아닌 `#key=`로 전달합니다. 앱이 읽은 뒤 주소에서 제거하고 브라우저 저장소에는 남기지 않으므로 새로고침 후에는 원래 링크로 다시 들어와야 합니다. 키를 받은 사람은 저장·재공유할 수 있습니다.

사진은 WebP를 지원하는 모바일/데스크톱 브라우저에서 볼 수 있습니다. 영상은 **AV1과 fMP4 MediaSource를 모두 지원하는 환경**이 필요합니다. 지원하지 않는 기기에서는 썸네일과 안내를 표시합니다. 모든 iPhone/Safari에서 재생된다고 보장하지 않습니다.

## 개발·검증

```sh
.prepare/venv/bin/python -m pip install -r script/requirements-dev.txt

# Python 테스트 + Node가 있으면 Python→Web Crypto 상호운용 검사
.prepare/venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# 짧은 무음 영상의 실제 AV1 변환 검사 포함
GALLERY_MEDIA_TESTS=1 .prepare/venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# 로컬 Chrome 설치 및 샘플 앨범 미리보기 서버 두 개 필요
.prepare/venv/bin/python script/preview.py 2026-10 --encrypted --port 8770
.prepare/venv/bin/python script/preview.py 2026-10 --port 8771 --non-interactive
.prepare/venv/bin/python tests/browser_smoke.py --key-file ../gallery-keys/2026-10.key
.prepare/venv/bin/python tests/browser_faults.py --key-file ../gallery-keys/2026-10.key
```

브라우저 smoke test는 제공된 2일/4개 샘플과 40초 이상 영상을 기준으로 작성되어 있습니다. 임의의 다른 앨범용 테스트는 아닙니다. 스크린샷과 결과는 루트 `.prepare/qa/`에 저장되어 Git에 포함되지 않습니다. 모바일 테스트는 Chrome의 화면·터치 에뮬레이션이며 실제 iOS/Android 코덱 검증을 대신하지 않습니다.

암호문 형식은 `GAL1` 4 bytes + nonce 12 bytes + ciphertext + GCM tag 16 bytes이며, AAD는 UTF-8 `gallery:v1|앨범ID|앨범기준상대경로`입니다. 키는 AES-256, 파일별 nonce는 OS 난수입니다. 공개된 리소스 파일명과 앨범 디렉터리를 임의로 바꾸면 인증이 실패합니다.
