# 🎙️ 교회 실시간 자막 테스트 프로토타입

교회 설교 시 실시간 한국어 자막 + 다국어 번역(영어/네팔어)을 테스트하기 위한 프로토타입입니다.

## 구성 요소

- **음성 인식**: CLOVA Speech (gRPC 실시간 스트리밍)
- **번역**: Google Gemini API (비동기)
- **UI**: 웹 기반 (FastAPI + WebSocket)
- **오디오**: sounddevice (마이크 실시간 캡처)

## 빠른 시작

### 1. 의존성 설치

```bash
pip install grpcio grpcio-tools google-genai fastapi uvicorn sounddevice
```

### 2. API 키 설정

`config.py`를 열어 아래 두 값을 실제 키로 교체하세요:

```python
CLOVA_SECRET = "실제_클로바_시크릿키"
GEMINI_API_KEY = "실제_제미나이_API키"
```

### 3. 실행

```bash
python main.py
```

브라우저가 자동으로 열립니다. **시작** 버튼을 누르면 자막이 시작됩니다.


## EXE 패키징 (배포용)

### 1. PyInstaller 설치

```bash
pip install pyinstaller
```

### 2. Proto 사전 컴파일

```bash
python -c "from main import ensure_proto_compiled; ensure_proto_compiled()"
```

### 3. EXE 빌드

```bash
pyinstaller --onefile --name church_subtitle --add-data "config.py;." --add-data "nest_pb2.py;." --add-data "nest_pb2_grpc.py;." --hidden-import=nest_pb2 --hidden-import=nest_pb2_grpc main.py
```

빌드 결과는 `dist/church_subtitle.exe`에 생성됩니다.

> **참고**: config.py에 API 키가 포함되어 있으므로 EXE 파일 관리에 주의하세요.


## 사용법

1. **마이크 선택**: 드롭다운에서 사용할 마이크를 선택 (기본 마이크 자동 선택)
2. **번역 언어**: 영어, 네팔어 중 원하는 언어를 체크
3. **모델 선택**: Gemini 모델 선택 (flash=빠름, pro=품질)
4. **시작/중지**: 버튼으로 제어

화면에는 최근 4개 문장이 표시되며, 한국어 원문과 번역이 함께 나옵니다.


## 파일 구조

```
church-subtitle/
├── main.py          # 메인 애플리케이션 (서버 + STT + 번역 + UI)
├── config.py        # API 키 및 설정
├── nest.proto       # (자동 생성) CLOVA gRPC Proto
├── nest_pb2.py      # (자동 생성) Proto 컴파일 결과
└── nest_pb2_grpc.py # (자동 생성) Proto 컴파일 결과
```


## 문제 해결

- **"마이크 감지 실패"**: sounddevice가 PortAudio를 찾을 수 없는 경우. Windows에서는 보통 자동 포함됨.
- **"STT 설정 실패"**: CLOVA Secret Key가 잘못되었거나 만료된 경우.
- **번역이 느린 경우**: `gemini-2.0-flash` 모델을 사용하세요. 네트워크 상태도 확인.
- **Proto 컴파일 실패**: `pip install grpcio-tools` 확인.
