#!/usr/bin/env python3
"""
교회 실시간 자막 테스트 프로토타입
- EXE 더블클릭 → 브라우저 자동 오픈 → 시작 버튼으로 바로 테스트
- CLOVA Speech gRPC 실시간 STT + Gemini 비동기 번역
- 한국어 + 영어/네팔어 자막 병기

사전 준비:
  pip install grpcio grpcio-tools google-genai fastapi uvicorn sounddevice jinja2

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
import pathlib
from dataclasses import dataclass
from typing import Optional

# ── 프로젝트 경로를 sys.path에 추가 (proto import용) ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config

# ── opus.dll 경로 등록 (opuslib의 find_library가 찾을 수 있도록) ──
_opus_dll_path = os.path.join(SCRIPT_DIR, "opus.dll")
if os.path.exists(_opus_dll_path):
    os.environ["PATH"] = SCRIPT_DIR + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(SCRIPT_DIR)


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
    """nest_pb2.py / nest_pb2_grpc.py 자동 컴파일 (EXE 환경에서는 스킵)"""
    if getattr(sys, "frozen", False):
        return

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

    def __init__(self, broadcast_fn, admin_ws, languages, model, mic_index,
                 broadcast_fn_audio=None):
        self.broadcast_fn = broadcast_fn
        self.broadcast_fn_audio = broadcast_fn_audio
        self.admin_ws = admin_ws
        self.languages = languages
        self.model = model
        self.mic_index = mic_index if mic_index >= 0 else None

        self.audio_queue = queue.Queue()
        self.audio_broadcast_queue = asyncio.Queue()
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

        # Opus 인코더 (opuslib 설치 시에만 활성화)
        self._opus_encoder = None
        try:
            import opuslib
            self._opus_encoder = opuslib.Encoder(
                config.AUDIO_SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP
            )
            self._opus_encoder.bitrate = config.OPUS_BITRATE
            print("[*] Opus 인코더 초기화 완료")
        except ImportError:
            print("[!] opuslib 미설치 — 오디오 스트리밍 비활성")
        except Exception as e:
            print(f"[!] Opus 인코더 초기화 실패: {e} — 오디오 스트리밍 비활성")

    async def start(self):
        """세션 시작: 마이크 캡처 → STT → 번역"""
        print(f"[STT] 세션 시작 (모델: {self.model}, 언어: {self.languages})")
        self.loop = asyncio.get_running_loop()

        # 마이크 시작
        try:
            self._start_audio()
            print(f"[STT] 마이크 캡처 시작 (index: {self.mic_index})")
        except Exception as e:
            print(f"[STT] 마이크 오류: {e}")
            await self._send({"type": "error", "message": f"마이크 오류: {e}"})
            return

        # gRPC 스트리밍 스레드 시작
        self.grpc_thread = threading.Thread(
            target=self._grpc_worker, daemon=True
        )
        self.grpc_thread.start()
        print(f"[STT] gRPC 워커 스레드 시작")

        await self._send({
            "type": "status",
            "state": "connecting",
            "message": "CLOVA STT 연결 중...",
        })

        # STT 결과 처리 + Opus 브로드캐스트 병렬 실행
        tasks = [self._process_stt_results()]
        if self._opus_encoder and self.broadcast_fn_audio:
            tasks.append(self._opus_broadcast_worker())
        await asyncio.gather(*tasks)

    async def stop(self):
        """세션 중지 및 리소스 정리"""
        self.stop_event.set()
        # Opus 워커 종료 센티넬
        try:
            self.audio_broadcast_queue.put_nowait(None)
        except Exception:
            pass

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
            blocksize=config.OPUS_FRAME_SAMPLES,
            dtype="int16",
            channels=config.AUDIO_CHANNELS,
            callback=self._audio_callback,
        )
        self.audio_stream.start()

    def _audio_callback(self, indata, frames, time_info, status):
        """오디오 콜백 → STT 큐 + 오디오 브로드캐스트 큐에 적재"""
        if not self.stop_event.is_set():
            chunk = bytes(indata)
            self.audio_queue.put(chunk)
            # Opus 브로드캐스트용 (asyncio 큐 — threadsafe)
            if self._opus_encoder and self.loop:
                self.loop.call_soon_threadsafe(
                    self.audio_broadcast_queue.put_nowait, chunk
                )

    # ── Opus 브로드캐스트 워커 ──

    async def _opus_broadcast_worker(self):
        """audio_broadcast_queue에서 PCM 청크를 받아 Opus 인코딩 후 브로드캐스트"""
        frame_bytes = config.OPUS_FRAME_BYTES
        frame_samples = config.OPUS_FRAME_SAMPLES
        frames_sent = 0

        while True:
            chunk = await self.audio_broadcast_queue.get()
            if chunk is None:  # 센티넬 — 종료
                break

            # 1초 PCM 청크를 20ms 프레임으로 분할하여 Opus 인코딩
            offset = 0
            while offset + frame_bytes <= len(chunk):
                pcm_frame = chunk[offset:offset + frame_bytes]
                offset += frame_bytes
                try:
                    opus_data = self._opus_encoder.encode(pcm_frame, frame_samples)
                    await self.broadcast_fn_audio(opus_data)
                    frames_sent += 1
                except Exception as e:
                    if frames_sent == 0:
                        print(f"[!] Opus 인코딩 오류: {e}")
                    break

            # 주기적 통계 (10초마다)
            if frames_sent > 0 and frames_sent % 500 == 0:
                print(f"[*] Opus: {frames_sent}프레임 전송됨")

    # ── gRPC 스트리밍 (별도 스레드) ──

    def _grpc_worker(self):
        """gRPC 스트리밍 워커 (동기 스레드)"""
        import grpc

        ensure_proto_compiled()
        import nest_pb2
        import nest_pb2_grpc

        print(f"[STT] CLOVA gRPC 연결 중... ({config.CLOVA_GRPC_HOST})")
        key_preview = config.CLOVA_SECRET[:8] + "..." if len(config.CLOVA_SECRET) > 8 else "(비어있음)"
        print(f"[STT] API 키: {key_preview}")

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
            # gRPC 에러에서 상세 정보 추출 + 사용자 친화적 메시지
            import grpc as _grpc
            if isinstance(e, _grpc.RpcError):
                code = e.code()
                details = e.details() or ""
                if code == _grpc.StatusCode.UNAUTHENTICATED:
                    error_msg = "CLOVA 인증 실패 — API 키(CLOVA_SECRET)를 확인하세요"
                elif code == _grpc.StatusCode.UNAVAILABLE:
                    error_msg = "CLOVA 서버 연결 불가 — 네트워크를 확인하세요"
                elif code == _grpc.StatusCode.PERMISSION_DENIED:
                    error_msg = "CLOVA 권한 거부 — API 키 권한을 확인하세요"
                elif code == _grpc.StatusCode.DEADLINE_EXCEEDED:
                    error_msg = "CLOVA 응답 시간 초과 — 네트워크 상태를 확인하세요"
                else:
                    error_msg = f"CLOVA STT 오류: {code.name} - {details}"
                print(f"[!] gRPC 오류: {code.name} - {details}")
            else:
                print(f"[!] STT 오류: {error_msg}")
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
                        "languages": self.languages,
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
        """관리자 + 모든 뷰어에게 전송"""
        msg_type = data.get("type")
        if msg_type in ("stt", "translation", "status"):
            # 뷰어 브로드캐스트
            await self.broadcast_fn(data)
            # 관리자에게도 전송
            try:
                await self.admin_ws.send_json(data)
            except Exception:
                pass
        else:
            # error / mics 등은 관리자에게만
            try:
                await self.admin_ws.send_json(data)
            except Exception:
                pass


# ══════════════════════════════════════════════
# 6. FastAPI 서버
# ══════════════════════════════════════════════

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from connection_manager import ConnectionManager

BASE_DIR = pathlib.Path(__file__).parent
# assets 폴더 경로 (exe 실행 시와 일반 실행 시 모두 대응)
ASSETS_DIR = BASE_DIR.parent / "assets"
if not ASSETS_DIR.exists():
    ASSETS_DIR = BASE_DIR / "assets"

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="석담교회 말씀 이음")

# 정적 파일 서빙 (아이콘 등)
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

manager = ConnectionManager()

# 현재 활성 세션
active_session: Optional[SubtitleSession] = None


@app.middleware("http")
async def admin_local_only(request: Request, call_next):
    """관리자 경로는 로컬호스트에서만 접근 허용"""
    if request.url.path.startswith("/admin") or request.url.path == "/ws/admin":
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            print(f"[보안] 관리자 접근 차단: {host} → {request.url.path}")
            return RedirectResponse(url="/")
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def viewer_page(request: Request):
    return templates.TemplateResponse("viewer.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/api/config")
async def api_config():
    """프론트엔드용 설정값 JSON"""
    return JSONResponse({
        "languages": config.LANGUAGE_CONFIGS,
        "models": config.GEMINI_MODELS,
        "default_model": config.DEFAULT_GEMINI_MODEL,
        "max_segments": config.MAX_DISPLAY_SENTENCES,
    })


def get_lan_ip():
    """PC의 실제 LAN IP 주소를 감지 (인터넷 미연결 시에도 동작 시도)"""
    import socket

    # 1. UDP 소켓을 통한 경로 탐색 (가장 정확한 주 인터페이스 탐색)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 실제로 데이터를 보내지는 않지만, 외부 경로를 통해 나가는 IP를 찾음
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass

    # 2. 호스트 이름을 통한 IP 조회 (인터넷 미연결 시 대비)
    try:
        hostname = socket.gethostname()
        # gethostbyname_ex를 사용해 여러 주소 중 루프백이 아닌 것을 선택
        _, _, addresses = socket.gethostbyname_ex(hostname)
        for addr in addresses:
            if not addr.startswith("127."):
                return addr
    except Exception:
        pass

    return "127.0.0.1"


@app.get("/api/network-info")
async def api_network_info():
    """내부망 IP 및 뷰어 URL 반환 (QR 코드 생성용)"""
    local_ip = get_lan_ip()
    port = config.SERVER_PORT
    viewer_url = f"http://{local_ip}:{port}/"

    return JSONResponse({
        "local_ip": local_ip,
        "port": port,
        "viewer_url": viewer_url,
    })


@app.get("/manifest.json")
async def manifest():
    """PWA 매니페스트"""
    return JSONResponse({
        "name": "석담교회 말씀 이음",
        "short_name": "말씀 이음",
        "description": "실시간 교회 자막 서비스",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#FAF6F0",
        "theme_color": "#8B2635",
        "icons": [
            {
                "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>✝</text></svg>",
                "sizes": "any",
                "type": "image/svg+xml",
            }
        ],
    })


# ── WebSocket: 관리자 ──

@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    global active_session

    client = websocket.client
    print(f"[WS] 관리자 연결 시도 (from {client.host}:{client.port})")
    await websocket.accept()
    await manager.connect_admin(websocket)
    print(f"[WS] 관리자 연결 성공")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")
            print(f"[WS] 관리자 액션: {action}")

            if action == "get_mics":
                try:
                    mics = get_microphones()
                    print(f"[WS] 마이크 {len(mics)}개 감지")
                except Exception as e:
                    mics = []
                    print(f"[WS] 마이크 감지 실패: {e}")
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

                manager.set_streaming_state(True, languages)

                # 뷰어에게 새 세션 시작 알림 → 클라이언트 세그먼트 초기화
                await manager.broadcast_to_viewers({"type": "session_start", "languages": languages})

                manager.set_audio_streaming(True)

                active_session = SubtitleSession(
                    broadcast_fn=manager.broadcast_to_viewers,
                    admin_ws=websocket,
                    languages=languages,
                    model=model,
                    mic_index=mic_index,
                    broadcast_fn_audio=manager.broadcast_audio,
                )
                asyncio.create_task(active_session.start())

            elif action == "stop":
                if active_session:
                    await active_session.stop()
                    active_session = None
                manager.set_streaming_state(False, [])
                manager.set_audio_streaming(False)

            elif action == "toggle_audio":
                enabled = msg.get("enabled", True)
                manager.set_audio_streaming(enabled)
                await websocket.send_json({
                    "type": "audio_status",
                    "enabled": manager.audio_streaming,
                })

    except WebSocketDisconnect:
        print("[WS] 관리자 연결 해제 (정상)")
    except Exception as e:
        import traceback
        print(f"[WS] 관리자 연결 오류: {e}")
        traceback.print_exc()
    finally:
        if active_session:
            await active_session.stop()
            active_session = None
        manager.set_streaming_state(False, [])
        manager.set_audio_streaming(False)
        await manager.disconnect_admin(websocket)
        print("[WS] 관리자 세션 정리 완료")


# ── WebSocket: 뷰어 ──

@app.websocket("/ws/viewer")
async def ws_viewer(websocket: WebSocket):
    await websocket.accept()
    await manager.connect_viewer(websocket)
    await manager.send_current_state(websocket)

    try:
        while True:
            # 30초 heartbeat (ping/pong 유지)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # 클라이언트 ping 응답
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # 서버→클라이언트 heartbeat
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect_viewer(websocket)


# ══════════════════════════════════════════════
# 7. 메인 엔트리
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
    missing = []
    if not config.CLOVA_SECRET or config.CLOVA_SECRET == "YOUR_CLOVA_SECRET_KEY_HERE":
        missing.append("CLOVA_SECRET")
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE":
        missing.append("GEMINI_API_KEY")

    if missing:
        print("=" * 50)
        print(f"  [!] API 키가 설정되지 않았습니다: {', '.join(missing)}")
        print()
        print("  .env 파일을 만들어 키를 입력하세요:")
        print(f"    위치: {os.path.join(SCRIPT_DIR, '.env')}")
        print()
        print("  .env 예시:")
        print('    CLOVA_SECRET=your_key_here')
        print('    GEMINI_API_KEY=your_key_here')
        print("=" * 50)
        input("  아무 키나 누르면 종료합니다...")
        sys.exit(1)

    host = config.SERVER_HOST
    port = config.SERVER_PORT
    admin_url = f"http://127.0.0.1:{port}"

    # LAN IP 감지 (콘솔 표시용)
    lan_ip = get_lan_ip()

    # 중복 인스턴스 확인
    if _is_port_in_use("127.0.0.1", port):
        print(f"[*] 이미 실행 중인 인스턴스 감지 (포트 {port})")
        print(f"[*] 기존 브라우저 탭에서 사용하거나, 기존 프로세스를 종료하세요.")
        print(f"[*] 브라우저를 열겠습니다...")
        webbrowser.open(f"{admin_url}/admin")
        sys.exit(0)

    print("=" * 50)
    print("  ✝ 석담교회 말씀 이음")
    print(f"  관리자: {admin_url}/admin")
    print(f"  뷰어:   http://{lan_ip}:{port}/")
    print("  종료: Ctrl+C")
    print("=" * 50)

    # 브라우저 자동 오픈 (약간의 딜레이)
    threading.Timer(1.5, lambda: webbrowser.open(f"{admin_url}/admin")).start()

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
