#!/usr/bin/env python3
"""
교회 실시간 자막 테스트 프로토타입
- EXE 더블클릭 → 브라우저 자동 오픈 → 시작 버튼으로 바로 테스트
- CLOVA Speech gRPC 실시간 STT + Gemini 비동기 번역
- 한국어 + 영어/네팔어 자막 병기

사전 준비:
  pip install grpcio grpcio-tools google-genai fastapi uvicorn sounddevice

실행:
  python main.py
"""

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Optional

# ── 프로젝트 경로를 sys.path에 추가 (proto import용) ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config


# ══════════════════════════════════════════════
# 1. Proto 자동 컴파일
# ══════════════════════════════════════════════

NEST_PROTO = """\
syntax = "proto3";
option java_multiple_files = true;
package com.nbp.cdncp.nest.grpc.proto.v1;

enum RequestType {
  CONFIG = 0;
  DATA = 1;
}

message NestConfig {
  string config = 1;
}

message NestData {
  bytes chunk = 1;
  string extra_contents = 2;
}

message NestRequest {
  RequestType type = 1;
  oneof part {
    NestConfig config = 2;
    NestData data = 3;
  }
}

message NestResponse {
  string contents = 1;
}

service NestService {
  rpc recognize(stream NestRequest) returns (stream NestResponse){};
}
"""


def ensure_proto_compiled():
    """nest_pb2.py / nest_pb2_grpc.py 자동 컴파일"""
    pb2_path = os.path.join(SCRIPT_DIR, "nest_pb2.py")
    pb2_grpc_path = os.path.join(SCRIPT_DIR, "nest_pb2_grpc.py")

    if os.path.exists(pb2_path) and os.path.exists(pb2_grpc_path):
        return

    print("[*] Proto 컴파일 중...")
    proto_path = os.path.join(SCRIPT_DIR, "nest.proto")
    with open(proto_path, "w") as f:
        f.write(NEST_PROTO)

    from grpc_tools import protoc

    result = protoc.main([
        "grpc_tools.protoc",
        f"-I={SCRIPT_DIR}",
        f"--python_out={SCRIPT_DIR}",
        f"--grpc_python_out={SCRIPT_DIR}",
        proto_path,
    ])

    if result != 0:
        print("[!] Proto 컴파일 실패. grpcio-tools 설치를 확인하세요.")
        sys.exit(1)
    print("[*] Proto 컴파일 완료")


# ══════════════════════════════════════════════
# 2. CLOVA STT Config 빌더
# ══════════════════════════════════════════════

def build_stt_config(language="ko"):
    """CLOVA Speech STT 설정 JSON 생성"""
    return json.dumps({
        "transcription": {"language": language},
        "semanticEpd": {
            "skipEmptyText": True,
            "useWordEpd": True,
            "usePeriodEpd": True,
            "gapThreshold": 2000,
            "durationThreshold": 20000,
            "syllableThreshold": 0,
        },
        "keywordBoosting": {
            "boostings": [{
                "words": config.CHURCH_KEYWORDS,
                "weight": 2.0,
            }],
        },
    }, ensure_ascii=False)


# ══════════════════════════════════════════════
# 3. Gemini 번역기
# ══════════════════════════════════════════════

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "refined_korean": {
            "type": "string",
            "description": "The refined/corrected Korean text with proper grammar and spacing.",
        },
        "translation": {
            "type": "string",
            "description": "The translated text in the target language.",
        },
    },
    "required": ["refined_korean", "translation"],
}


@dataclass
class TranslationResult:
    original: str
    refined_korean: str = ""
    translated: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None


class GeminiTranslator:
    """Gemini API 비동기 번역기 (다국어 지원)"""

    def __init__(self, model: str = config.DEFAULT_GEMINI_MODEL):
        self.model = model
        os.environ["GEMINI_API_KEY"] = config.GEMINI_API_KEY

        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client()

    @staticmethod
    def _try_repair_json(text: str) -> dict | None:
        """잘린 JSON을 복구하여 파싱 시도.

        잘린 패턴 예시:
          {"refined_korean": "완전한 텍스트", "translation": "Truncated tex
          {"refined_korean": "완전한 텍스트", "translation": "Complete text"
          {"refined_korean": "텍스트만 있고
        """
        import re

        # 빈 문자열이면 패스
        text = text.strip()
        if not text or not text.startswith("{"):
            return None

        # 점진적으로 닫는 문자를 붙여가며 파싱 시도
        # 가장 그럴듯한 복구 순서대로
        repairs = ['"}', '"}', '"}\n}', '"}]', '']
        for suffix in repairs:
            candidate = text + suffix
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue

        # 정규식으로 각 필드를 개별 추출 (닫는 따옴표 없어도 가능)
        result = {}
        for field in ("refined_korean", "translation"):
            # 완전한 값 (닫는 따옴표 있음)
            m = re.search(
                rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"',
                text, re.DOTALL
            )
            if m:
                val = m.group(1)
            else:
                # 잘린 값 (닫는 따옴표 없음 — 문자열 끝까지)
                m = re.search(
                    rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)',
                    text, re.DOTALL
                )
                if m:
                    val = m.group(1)
                else:
                    continue
            val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            result[field] = val.strip()

        return result if result else None

    @staticmethod
    def _extract_response(raw: str) -> dict:
        """Gemini 응답에서 refined_korean과 translation을 견고하게 추출.

        처리 케이스:
          1. 정상 JSON
          2. 코드펜스 래핑
          3. 잘린(truncated) JSON — max_output_tokens 초과 시
          4. 키 누락 / 이스케이프

        Returns:
            {"refined_korean": "...", "translation": "..."}
        """
        import re

        result = {"refined_korean": "", "translation": ""}
        text = raw.strip()
        if not text:
            return result

        # 1단계: 마크다운 코드펜스 제거
        fence_match = re.search(
            r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL
        )
        if fence_match:
            text = fence_match.group(1).strip()

        # 2단계: 정상 JSON 파싱
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                result["refined_korean"] = str(parsed.get("refined_korean", ""))
                result["translation"] = str(parsed.get("translation", ""))
                if result["translation"]:
                    return result
                for k, v in parsed.items():
                    if isinstance(v, str) and v.strip() and k != "refined_korean":
                        result["translation"] = v
                        break
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # 3단계: 잘린 JSON 복구
        repaired = GeminiTranslator._try_repair_json(text)
        if repaired:
            result["refined_korean"] = repaired.get("refined_korean", "")
            result["translation"] = repaired.get("translation", "")

        return result

    async def translate(self, text: str, target_lang: str, context: list[str] = None) -> TranslationResult:
        """한국어 텍스트를 대상 언어로 비동기 번역 (이전 문장 맥락 포함)"""
        result = TranslationResult(original=text)
        if not text.strip():
            return result

        lang_cfg = config.LANGUAGE_CONFIGS.get(target_lang)
        if not lang_cfg:
            result.error = f"지원하지 않는 언어: {target_lang}"
            result.translated = f"[미지원 언어: {target_lang}]"
            return result

        # 맥락이 있으면 프롬프트에 이전 문장 포함
        if context:
            context_block = "\n".join(f"- {s}" for s in context)
            prompt = (
                f"[Previous sentences for context]\n{context_block}\n\n"
                f"[Translate this sentence]\n{text}"
            )
        else:
            prompt = text

        start = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=lang_cfg["system_prompt"],
                    response_mime_type="application/json",
                    response_schema=TRANSLATION_SCHEMA,
                    temperature=0.3,
                    max_output_tokens=1024,  # refined_korean + translation 두 필드 충분히
                ),
            )
            result.latency_ms = (time.perf_counter() - start) * 1000

            raw = response.text if response and response.text else ""
            extracted = self._extract_response(raw)
            result.translated = extracted["translation"]
            result.refined_korean = extracted["refined_korean"]

            if not result.translated:
                result.translated = "[빈 응답]"
                result.error = "Gemini 빈 응답"

        except Exception as e:
            result.latency_ms = (time.perf_counter() - start) * 1000
            result.error = str(e)
            result.translated = "[번역 오류]"

        return result


# ══════════════════════════════════════════════
# 4. 마이크 장치 목록
# ══════════════════════════════════════════════

def get_microphones():
    """사용 가능한 마이크 목록 반환"""
    import sounddevice as sd

    devices = sd.query_devices()
    mics = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            mics.append({
                "index": i,
                "name": d["name"],
                "is_default": i == sd.default.device[0],
            })
    return mics


# ══════════════════════════════════════════════
# 5. 자막 세션 (STT + 번역 파이프라인)
# ══════════════════════════════════════════════

class SubtitleSession:
    """하나의 자막 세션 (시작~중지)을 관리"""

    def __init__(self, websocket, languages, model, mic_index):
        self.ws = websocket
        self.languages = languages
        self.model = model
        self.mic_index = mic_index if mic_index >= 0 else None

        self.audio_queue = queue.Queue()
        self.stt_queue = asyncio.Queue()
        self.stop_event = threading.Event()
        self.loop = None
        self.audio_stream = None
        self.grpc_thread = None
        self.segment_id = 0

        # 번역 맥락 유지: 최근 N문장을 컨텍스트로 전달
        self.recent_texts: list[str] = []
        self.recent_seg_ids: list[int] = []  # recent_texts와 1:1 매핑
        self.refined_segments: set[int] = set()  # 이미 refined된 segment id
        self.CONTEXT_SIZE = 3  # 앞선 3문장을 참고로 전달

        self.translator = GeminiTranslator(model=model)

    async def start(self):
        """세션 시작: 마이크 캡처 → STT → 번역"""
        self.loop = asyncio.get_running_loop()

        # 마이크 시작
        try:
            self._start_audio()
        except Exception as e:
            await self._send({"type": "error", "message": f"마이크 오류: {e}"})
            return

        # gRPC 스트리밍 스레드 시작
        self.grpc_thread = threading.Thread(
            target=self._grpc_worker, daemon=True
        )
        self.grpc_thread.start()

        await self._send({
            "type": "status",
            "state": "connecting",
            "message": "CLOVA STT 연결 중...",
        })

        # STT 결과 처리 루프
        await self._process_stt_results()

    async def stop(self):
        """세션 중지 및 리소스 정리"""
        self.stop_event.set()

        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None

        # gRPC 스레드 종료 대기 (최대 2초)
        if self.grpc_thread and self.grpc_thread.is_alive():
            self.grpc_thread.join(timeout=2)

        await self._send({
            "type": "status",
            "state": "stopped",
            "message": "중지됨",
        })

    def _start_audio(self):
        """sounddevice로 마이크 캡처 시작"""
        import sounddevice as sd

        self.audio_stream = sd.RawInputStream(
            device=self.mic_index,
            samplerate=config.AUDIO_SAMPLE_RATE,
            blocksize=config.AUDIO_SAMPLE_RATE,  # 1초 단위
            dtype="int16",
            channels=config.AUDIO_CHANNELS,
            callback=self._audio_callback,
        )
        self.audio_stream.start()

    def _audio_callback(self, indata, frames, time_info, status):
        """오디오 콜백 → 청크를 큐에 적재"""
        if not self.stop_event.is_set():
            self.audio_queue.put(bytes(indata))

    # ── gRPC 스트리밍 (별도 스레드) ──

    def _grpc_worker(self):
        """gRPC 스트리밍 워커 (동기 스레드)"""
        import grpc

        ensure_proto_compiled()
        import nest_pb2
        import nest_pb2_grpc

        channel = grpc.secure_channel(
            config.CLOVA_GRPC_HOST,
            grpc.ssl_channel_credentials(),
        )
        stub = nest_pb2_grpc.NestServiceStub(channel)
        metadata = (("authorization", f"Bearer {config.CLOVA_SECRET}"),)

        try:
            responses = stub.recognize(
                self._generate_requests(nest_pb2),
                metadata=metadata,
            )
            for resp in responses:
                if self.stop_event.is_set():
                    break
                data = json.loads(resp.contents)
                self.loop.call_soon_threadsafe(
                    self.stt_queue.put_nowait, data
                )
        except Exception as e:
            error_msg = str(e)
            # gRPC 에러에서 상세 정보 추출
            import grpc as _grpc
            if isinstance(e, _grpc.RpcError):
                error_msg = f"{e.code()} - {e.details()}"
            self.loop.call_soon_threadsafe(
                self.stt_queue.put_nowait, {"error": error_msg}
            )
        finally:
            channel.close()
            # 종료 시그널
            self.loop.call_soon_threadsafe(
                self.stt_queue.put_nowait, None
            )

    def _generate_requests(self, nest_pb2):
        """gRPC 스트리밍 요청 제너레이터"""
        config_json = build_stt_config("ko")

        # Config 전송
        yield nest_pb2.NestRequest(
            type=nest_pb2.RequestType.CONFIG,
            config=nest_pb2.NestConfig(config=config_json),
        )

        # 오디오 청크 전송
        seq = 0
        while not self.stop_event.is_set():
            try:
                chunk = self.audio_queue.get(timeout=0.5)
                seq += 1
                yield nest_pb2.NestRequest(
                    type=nest_pb2.RequestType.DATA,
                    data=nest_pb2.NestData(
                        chunk=chunk,
                        extra_contents=json.dumps({
                            "seqId": seq,
                            "epFlag": False,
                        }),
                    ),
                )
            except queue.Empty:
                continue

    # ── STT 결과 처리 (async) ──

    async def _process_stt_results(self):
        """STT 결과를 수신하고 번역을 트리거"""
        while True:
            try:
                data = await asyncio.wait_for(
                    self.stt_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if self.stop_event.is_set():
                    break
                continue

            if data is None:  # 종료 시그널
                break

            if "error" in data:
                await self._send({
                    "type": "error",
                    "message": str(data["error"]),
                })
                break

            response_type = data.get("responseType", [])

            # Config 응답
            if "config" in response_type:
                status = data.get("config", {}).get("status", "")
                if status == "Success":
                    await self._send({
                        "type": "status",
                        "state": "running",
                        "message": "인식 중...",
                    })
                else:
                    await self._send({
                        "type": "error",
                        "message": f"STT 설정 실패: {status}",
                    })
                    break

            # 인식 결과 → 번역
            if "transcription" in response_type:
                tr = data.get("transcription", {})
                text = tr.get("text", "").strip()
                confidence = tr.get("confidence", 0)

                if text:
                    self.segment_id += 1
                    seg_id = self.segment_id

                    # 한국어 텍스트 즉시 전송
                    await self._send({
                        "type": "stt",
                        "segment_id": seg_id,
                        "text": text,
                        "confidence": round(confidence, 4),
                    })

                    # 맥락용 이전 문장들 스냅샷
                    context = list(self.recent_texts)

                    # 히스토리에 현재 문장 추가 (seg_id와 함께)
                    self.recent_texts.append(text)
                    self.recent_seg_ids.append(seg_id)
                    if len(self.recent_texts) > self.CONTEXT_SIZE:
                        self.recent_texts.pop(0)
                        self.recent_seg_ids.pop(0)

                    # 각 언어별 번역 태스크 생성
                    for lang in self.languages:
                        asyncio.create_task(
                            self._translate_and_send(seg_id, text, lang, context)
                        )

    async def _translate_and_send(self, segment_id, text, lang, context=None):
        """번역 수행 후 WebSocket으로 전송 (다듬어진 한국어 포함)"""
        result = await self.translator.translate(text, lang, context)

        # 다듬어진 한국어가 있으면 맥락 히스토리도 갱신 (최초 1회만)
        if result.refined_korean and segment_id not in self.refined_segments:
            self.refined_segments.add(segment_id)
            try:
                idx = self.recent_seg_ids.index(segment_id)
                self.recent_texts[idx] = result.refined_korean
            except ValueError:
                pass  # 이미 히스토리에서 밀려난 경우

        await self._send({
            "type": "translation",
            "segment_id": segment_id,
            "lang": lang,
            "text": result.translated,
            "refined_ko": result.refined_korean,
            "latency_ms": round(result.latency_ms, 0),
            "error": result.error,
        })

    async def _send(self, data):
        """WebSocket으로 JSON 전송 (에러 무시)"""
        try:
            await self.ws.send_json(data)
        except Exception:
            pass


# ══════════════════════════════════════════════
# 6. HTML 템플릿 (인라인)
# ══════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>교회 자막 테스트</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #0c0c14;
    color: #e0e0e0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* ── 헤더 (컨트롤) ── */
  .header {
    background: #161625;
    border-bottom: 1px solid #2a2a3e;
    padding: 16px 24px;
  }

  .header h1 {
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
  }

  .control-group {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .control-group label {
    font-size: 13px;
    color: #888;
    white-space: nowrap;
  }

  select {
    background: #0c0c14;
    color: #e0e0e0;
    border: 1px solid #3a3a50;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    font-family: inherit;
    cursor: pointer;
    outline: none;
  }
  select:focus { border-color: #6366f1; }

  .checkbox-group {
    display: flex;
    gap: 10px;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 13px;
    cursor: pointer;
    user-select: none;
  }

  .checkbox-label input[type="checkbox"] {
    accent-color: #6366f1;
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .btn-group {
    display: flex;
    gap: 8px;
    margin-left: auto;
  }

  .btn {
    padding: 8px 20px;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
    transition: opacity 0.2s;
  }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .btn-start {
    background: #22c55e;
    color: #000;
  }
  .btn-stop {
    background: #ef4444;
    color: #fff;
  }

  /* ── 자막 영역 ── */
  .subtitle-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    padding: 24px;
    gap: 12px;
    min-height: 400px;
  }

  .segment {
    background: #1a1a2e;
    border-radius: 10px;
    padding: 16px 20px;
    border-left: 3px solid #6366f1;
    animation: fadeIn 0.3s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .seg-ko {
    font-size: 20px;
    font-weight: 700;
    color: #fff;
    line-height: 1.5;
    margin-bottom: 6px;
  }

  .seg-translation {
    font-size: 17px;
    line-height: 1.5;
    margin-top: 4px;
  }

  .seg-translation.en { color: #7dd3fc; }
  .seg-translation.ne { color: #86efac; }
  .seg-translation.pending { color: #555; font-style: italic; }

  .seg-meta {
    font-size: 11px;
    color: #555;
    margin-top: 6px;
  }

  .refined-badge {
    font-size: 12px;
    color: #a78bfa;
    cursor: help;
  }

  .empty-state {
    text-align: center;
    color: #444;
    font-size: 15px;
    margin: auto;
  }

  /* ── 상태바 ── */
  .status-bar {
    background: #161625;
    border-top: 1px solid #2a2a3e;
    padding: 8px 24px;
    display: flex;
    gap: 20px;
    font-size: 12px;
    color: #666;
    align-items: center;
  }

  .status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 4px;
  }
  .dot-ready { background: #888; }
  .dot-connecting { background: #f59e0b; animation: pulse 1s infinite; }
  .dot-running { background: #22c55e; animation: pulse 1.5s infinite; }
  .dot-error { background: #ef4444; }
  .dot-stopped { background: #888; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
</style>
</head>
<body>

<!-- 헤더 -->
<div class="header">
  <h1>🎙️ 교회 자막 테스트</h1>
  <div class="controls">
    <div class="control-group">
      <label>마이크:</label>
      <select id="micSelect"><option value="-1">로딩 중...</option></select>
    </div>
    <div class="control-group">
      <label>번역:</label>
      <div class="checkbox-group" id="langCheckboxes"></div>
    </div>
    <div class="control-group">
      <label>모델:</label>
      <select id="modelSelect"></select>
    </div>
    <div class="btn-group">
      <button class="btn btn-start" id="btnStart" onclick="startSession()">● 시작</button>
      <button class="btn btn-stop" id="btnStop" onclick="stopSession()" disabled>■ 중지</button>
    </div>
  </div>
</div>

<!-- 자막 -->
<div class="subtitle-area" id="subtitleArea">
  <div class="empty-state" id="emptyState">
    시작 버튼을 눌러 자막 테스트를 시작하세요
  </div>
</div>

<!-- 상태바 -->
<div class="status-bar">
  <span id="statusText">
    <span class="status-dot dot-ready" id="statusDot"></span>
    <span id="statusMsg">대기 중</span>
  </span>
  <span id="statConfidence"></span>
  <span id="statLatency"></span>
</div>

<script>
// ── 설정 ──
const MAX_SEGMENTS = """ + str(config.MAX_DISPLAY_SENTENCES) + """;
const LANGUAGES = """ + json.dumps(config.LANGUAGE_CONFIGS, ensure_ascii=False) + """;
const MODELS = """ + json.dumps(config.GEMINI_MODELS) + """;
const DEFAULT_MODEL = """ + json.dumps(config.DEFAULT_GEMINI_MODEL) + """;

// ── 상태 ──
let ws = null;
let segments = [];
let selectedLanguages = [];
let isRunning = false;

// ── 초기화 ──
window.addEventListener('load', () => {
  initLanguageCheckboxes();
  initModelSelect();
  connectWebSocket();
});

function initLanguageCheckboxes() {
  const container = document.getElementById('langCheckboxes');
  for (const [code, cfg] of Object.entries(LANGUAGES)) {
    const label = document.createElement('label');
    label.className = 'checkbox-label';
    label.innerHTML = `<input type="checkbox" value="${code}" checked> ${cfg.flag} ${cfg.name}`;
    container.appendChild(label);
  }
}

function initModelSelect() {
  const sel = document.getElementById('modelSelect');
  MODELS.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    if (m === DEFAULT_MODEL) opt.selected = true;
    sel.appendChild(opt);
  });
}

function getSelectedLanguages() {
  const checks = document.querySelectorAll('#langCheckboxes input:checked');
  return Array.from(checks).map(c => c.value);
}

// ── WebSocket ──
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    updateStatus('ready', '대기 중');
    ws.send(JSON.stringify({ action: 'get_mics' }));
  };

  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    handleMessage(data);
  };

  ws.onclose = () => {
    updateStatus('error', '연결 끊김');
    if (isRunning) {
      isRunning = false;
      updateButtons();
    }
    // 3초 후 재연결
    setTimeout(connectWebSocket, 3000);
  };

  ws.onerror = () => {
    updateStatus('error', '연결 오류');
  };
}

function handleMessage(data) {
  switch (data.type) {
    case 'mics':
      populateMics(data.devices);
      break;
    case 'status':
      updateStatus(data.state, data.message);
      break;
    case 'stt':
      addSegment(data.segment_id, data.text, data.confidence);
      break;
    case 'translation':
      addTranslation(data.segment_id, data.lang, data.text, data.latency_ms, data.error, data.refined_ko);
      break;
    case 'error':
      updateStatus('error', data.message);
      break;
  }
}

function populateMics(devices) {
  const sel = document.getElementById('micSelect');
  sel.innerHTML = '';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.index;
    opt.textContent = d.name + (d.is_default ? ' (기본)' : '');
    if (d.is_default) opt.selected = true;
    sel.appendChild(opt);
  });
}

// ── 세션 제어 ──
function startSession() {
  selectedLanguages = getSelectedLanguages();
  if (selectedLanguages.length === 0) {
    alert('번역할 언어를 하나 이상 선택하세요.');
    return;
  }

  segments = [];
  renderSubtitles();

  const msg = {
    action: 'start',
    mic_index: parseInt(document.getElementById('micSelect').value),
    languages: selectedLanguages,
    model: document.getElementById('modelSelect').value,
  };
  ws.send(JSON.stringify(msg));

  isRunning = true;
  updateButtons();
}

function stopSession() {
  ws.send(JSON.stringify({ action: 'stop' }));
  isRunning = false;
  updateButtons();
}

function updateButtons() {
  document.getElementById('btnStart').disabled = isRunning;
  document.getElementById('btnStop').disabled = !isRunning;
  // 실행 중에는 설정 변경 불가
  document.getElementById('micSelect').disabled = isRunning;
  document.getElementById('modelSelect').disabled = isRunning;
  document.querySelectorAll('#langCheckboxes input').forEach(c => c.disabled = isRunning);
}

// ── 자막 표시 ──
function addSegment(segmentId, koreanText, confidence) {
  segments.push({
    id: segmentId,
    ko: koreanText,
    ko_raw: koreanText,
    ko_refined: false,
    confidence: confidence,
    translations: {},
  });
  if (segments.length > MAX_SEGMENTS) segments.shift();

  document.getElementById('statConfidence').textContent = `신뢰도: ${confidence.toFixed(3)}`;
  renderSubtitles();
}

function addTranslation(segmentId, lang, text, latencyMs, error, refinedKo) {
  const seg = segments.find(s => s.id === segmentId);
  if (seg) {
    seg.translations[lang] = { text, latency: latencyMs, error };

    // 다듬어진 한국어가 있고 아직 교체 안 된 경우 → 원문 교체
    if (refinedKo && refinedKo.trim() && !seg.ko_refined) {
      seg.ko = refinedKo;
      seg.ko_refined = true;
    }

    document.getElementById('statLatency').textContent = `번역지연: ${Math.round(latencyMs)}ms`;
    renderSubtitles();
  }
}

function renderSubtitles() {
  const area = document.getElementById('subtitleArea');
  const empty = document.getElementById('emptyState');

  if (segments.length === 0) {
    empty.style.display = '';
    area.querySelectorAll('.segment').forEach(el => el.remove());
    return;
  }
  empty.style.display = 'none';

  // 전체 재렌더링 (간단한 구현)
  area.querySelectorAll('.segment').forEach(el => el.remove());

  segments.forEach(seg => {
    const div = document.createElement('div');
    div.className = 'segment';

    // 한국어 원문 (다듬어진 경우 ✎ 표시, 원본은 툴팁)
    const koLabel = seg.ko_refined
      ? `<span class="refined-badge" title="원본: ${escapeAttr(seg.ko_raw)}">✎</span> `
      : '';
    let html = `<div class="seg-ko">${koLabel}${escapeHtml(seg.ko)}</div>`;

    selectedLanguages.forEach(lang => {
      const tr = seg.translations[lang];
      const cfg = LANGUAGES[lang];
      if (tr) {
        const cls = tr.error ? 'pending' : lang;
        html += `<div class="seg-translation ${cls}">${cfg.flag} ${escapeHtml(tr.text)}</div>`;
      } else {
        html += `<div class="seg-translation pending">${cfg.flag} 번역 중...</div>`;
      }
    });

    const latencies = Object.values(seg.translations)
      .filter(t => t.latency)
      .map(t => `${Math.round(t.latency)}ms`);
    const metaText = `신뢰도 ${seg.confidence.toFixed(3)}` +
      (latencies.length ? ` · 번역 ${latencies.join(' / ')}` : '');
    html += `<div class="seg-meta">${metaText}</div>`;

    div.innerHTML = html;
    area.appendChild(div);
  });

  // 스크롤 하단
  area.scrollTop = area.scrollHeight;
}

// ── 유틸 ──
function updateStatus(state, message) {
  const dot = document.getElementById('statusDot');
  const msg = document.getElementById('statusMsg');
  dot.className = `status-dot dot-${state}`;
  msg.textContent = message;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function escapeAttr(str) {
  return str.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════
# 7. FastAPI 서버
# ══════════════════════════════════════════════

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="교회 자막 테스트")

# 현재 활성 세션
active_session: Optional[SubtitleSession] = None


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_TEMPLATE


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global active_session

    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "get_mics":
                try:
                    mics = get_microphones()
                except Exception as e:
                    mics = []
                    await websocket.send_json({
                        "type": "error",
                        "message": f"마이크 감지 실패: {e}",
                    })
                await websocket.send_json({"type": "mics", "devices": mics})

            elif action == "start":
                # 기존 세션 정리
                if active_session:
                    await active_session.stop()

                languages = msg.get("languages", ["en"])
                model = msg.get("model", config.DEFAULT_GEMINI_MODEL)
                mic_index = msg.get("mic_index", -1)

                active_session = SubtitleSession(
                    websocket=websocket,
                    languages=languages,
                    model=model,
                    mic_index=mic_index,
                )
                # 세션을 백그라운드 태스크로 실행
                asyncio.create_task(active_session.start())

            elif action == "stop":
                if active_session:
                    await active_session.stop()
                    active_session = None

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS 오류] {e}")
    finally:
        if active_session:
            await active_session.stop()
            active_session = None


# ══════════════════════════════════════════════
# 8. 메인 엔트리
# ══════════════════════════════════════════════

def _is_port_in_use(host, port):
    """포트가 이미 사용 중인지 확인"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def main():
    # Proto 사전 컴파일
    ensure_proto_compiled()

    # API 키 확인
    if config.CLOVA_SECRET == "YOUR_CLOVA_SECRET_KEY_HERE":
        print("=" * 50)
        print("  [!] config.py에 CLOVA_SECRET을 설정하세요.")
        print("=" * 50)
        sys.exit(1)

    if config.GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE":
        print("=" * 50)
        print("  [!] config.py에 GEMINI_API_KEY를 설정하세요.")
        print("=" * 50)
        sys.exit(1)

    host = config.SERVER_HOST
    port = config.SERVER_PORT
    url = f"http://{host}:{port}"

    # 중복 인스턴스 확인
    if _is_port_in_use(host, port):
        print(f"[*] 이미 실행 중인 인스턴스 감지 (포트 {port})")
        print(f"[*] 기존 브라우저 탭에서 사용하거나, 기존 프로세스를 종료하세요.")
        print(f"[*] 브라우저를 열겠습니다...")
        webbrowser.open(url)
        sys.exit(0)

    print("=" * 50)
    print("  🎙️ 교회 자막 테스트 프로토타입")
    print(f"  서버: {url}")
    print("  종료: Ctrl+C")
    print("=" * 50)

    # 브라우저 자동 오픈 (약간의 딜레이)
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()