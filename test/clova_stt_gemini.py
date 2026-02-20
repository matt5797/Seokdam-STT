#!/usr/bin/env python3
"""
CLOVA Speech 실시간 스트리밍 STT + Gemini 비동기 번역 테스트
- 교회 자막/번역 서비스 도입을 위한 성능 평가용
- PCM 16kHz / mono / 16bit 음성 파일 기반 테스트
- STT: CLOVA Speech (gRPC 스트리밍)
- 번역: Google Gemini API (비동기, structured output)

사전 준비:
  1. pip install grpcio grpcio-tools google-genai
  2. 테스트 음원을 PCM으로 변환:
     ffmpeg -i input.mp3 -ar 16000 -ac 1 -f s16le output.pcm
  3. CLOVA Speech Basic 장문인식 플랜 Secret Key 발급
  4. Gemini API Key 발급 (https://aistudio.google.com/)

사용법:
  export GEMINI_API_KEY=your_gemini_key
  python clova_stt_gemini_translate_test.py --secret CLOVA_SECRET --audio sermon.pcm

  또는:
  python clova_stt_gemini_translate_test.py \
      --secret CLOVA_SECRET \
      --audio sermon.pcm \
      --gemini-key YOUR_GEMINI_KEY
"""

import argparse
import asyncio
import json
import os
import sys
import time
import textwrap
import threading
from dataclasses import dataclass, field
from typing import Optional

# ──────────────────────────────────────────────
# 1. Proto 자동 컴파일
# ──────────────────────────────────────────────

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
    """nest_pb2.py / nest_pb2_grpc.py가 없으면 자동 컴파일"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pb2_path = os.path.join(script_dir, "nest_pb2.py")
    pb2_grpc_path = os.path.join(script_dir, "nest_pb2_grpc.py")

    if os.path.exists(pb2_path) and os.path.exists(pb2_grpc_path):
        return

    print("[*] nest_pb2 파일이 없습니다. Proto 컴파일을 진행합니다...")

    proto_path = os.path.join(script_dir, "nest.proto")
    with open(proto_path, "w") as f:
        f.write(NEST_PROTO)

    from grpc_tools import protoc
    result = protoc.main([
        "grpc_tools.protoc",
        f"-I={script_dir}",
        f"--python_out={script_dir}",
        f"--grpc_python_out={script_dir}",
        proto_path,
    ])

    if result != 0:
        print("[!] Proto 컴파일 실패. grpcio-tools가 설치되어 있는지 확인하세요.")
        print("    pip install grpcio grpcio-tools")
        sys.exit(1)

    print("[*] Proto 컴파일 완료\n")


# ──────────────────────────────────────────────
# 2. CLOVA STT Config (번역 제거, STT + 키워드부스팅만)
# ──────────────────────────────────────────────

CHURCH_KEYWORDS = (
    "하나님,예수,그리스도,성령,아멘,할렐루야,"
    "말씀,은혜,찬양,기도,축복,구원,십자가,부활,"
    "성경,복음,믿음,소망,사랑,회개,세례,성찬,"
    "목사,장로,집사,권사,성도"
)


def build_stt_config(language="ko"):
    """CLOVA STT Config JSON (번역 없이 STT만)"""
    config = {
        "transcription": {
            "language": language,
        },
        "semanticEpd": {
            "skipEmptyText": True,
            "useWordEpd": True,
            "usePeriodEpd": True,
            "gapThreshold": 2000,
            "durationThreshold": 20000,
            "syllableThreshold": 0,
        },
        "keywordBoosting": {
            "boostings": [
                {
                    "words": CHURCH_KEYWORDS,
                    "weight": 2.0,
                }
            ],
        },
    }
    return json.dumps(config, ensure_ascii=False)


# ──────────────────────────────────────────────
# 3. Gemini 비동기 번역 모듈
# ──────────────────────────────────────────────

# 번역 JSON 스키마 (structured output)
TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "translation": {
            "type": "string",
            "description": "The English translation of the Korean text.",
        },
    },
    "required": ["translation"],
}

# 교회 설교 번역에 최적화된 시스템 프롬프트
TRANSLATION_SYSTEM_PROMPT = (
    "You are a professional translator specializing in Korean Protestant church sermons. "
    "Translate the given Korean text into natural, fluent English. "
    "Preserve religious terminology accurately (e.g., 하나님→God, 예수→Jesus, "
    "성령→Holy Spirit, 말씀→the Word, 은혜→grace, 십자가→the cross). "
    "Keep the translation concise and suitable for real-time subtitles. "
    "Respond ONLY with the JSON object, no extra text."
)


@dataclass
class TranslationResult:
    """번역 결과를 담는 데이터 클래스"""
    original: str
    translated: str = ""
    latency_ms: float = 0.0
    start_ts: int = 0
    end_ts: int = 0
    confidence: float = 0.0
    error: Optional[str] = None


class GeminiTranslator:
    """Gemini API 비동기 번역기"""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.0-flash"):
        self.model = model

        # API Key 설정: 인자 > 환경변수
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key

        if not os.environ.get("GEMINI_API_KEY"):
            raise ValueError(
                "Gemini API Key가 필요합니다. "
                "--gemini-key 인자 또는 GEMINI_API_KEY 환경변수를 설정하세요."
            )

        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self._client = genai.Client()

    async def translate(self, text: str) -> TranslationResult:
        """한국어 텍스트를 영어로 비동기 번역"""
        result = TranslationResult(original=text)

        if not text.strip():
            result.translated = ""
            return result

        start = time.perf_counter()

        try:
            # Gemini API는 동기 SDK이므로 asyncio.to_thread로 감싸서 비동기 처리
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model,
                contents=text,
                config=self._types.GenerateContentConfig(
                    system_instruction=TRANSLATION_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=TRANSLATION_SCHEMA,
                    temperature=0.3,  # 번역은 일관성이 중요하므로 낮은 temperature
                    max_output_tokens=512,
                ),
            )

            elapsed_ms = (time.perf_counter() - start) * 1000
            result.latency_ms = elapsed_ms

            # JSON 파싱
            raw = response.text.strip()
            parsed = json.loads(raw)
            result.translated = parsed.get("translation", raw)

        except json.JSONDecodeError:
            # JSON 파싱 실패 시 원본 텍스트 그대로 사용
            result.translated = response.text.strip() if response else ""
            result.error = "JSON 파싱 실패 (원본 텍스트 사용)"

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            result.latency_ms = elapsed_ms
            result.error = str(e)
            result.translated = f"[번역 실패: {e}]"

        return result


# ──────────────────────────────────────────────
# 4. gRPC 스트리밍 + 비동기 번역 통합
# ──────────────────────────────────────────────

CHUNK_SIZE = 32000  # 1초 분량
GRPC_HOST = "clovaspeech-gw.ncloud.com:50051"


def generate_requests(audio_path, config_json):
    """Config 전송 후 PCM 청크를 순차 스트리밍"""
    import nest_pb2

    yield nest_pb2.NestRequest(
        type=nest_pb2.RequestType.CONFIG,
        config=nest_pb2.NestConfig(config=config_json),
    )

    with open(audio_path, "rb") as f:
        seq = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
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
            time.sleep(len(chunk) / CHUNK_SIZE)

    print("\n[*] 오디오 전송 완료. 남은 응답 대기 중...\n")


async def run_test(secret_key, audio_path, language, gemini_key, gemini_model):
    """STT + 비동기 번역 통합 테스트"""
    import grpc
    import nest_pb2_grpc

    # Gemini 번역기 초기화
    translator = GeminiTranslator(api_key=gemini_key, model=gemini_model)

    config_json = build_stt_config(language)

    print("=" * 64)
    print("  CLOVA Speech STT + Gemini 번역 테스트")
    print("=" * 64)
    print(f"  음성 파일     : {audio_path}")
    print(f"  파일 크기     : {os.path.getsize(audio_path):,} bytes")
    print(f"  예상 길이     : {os.path.getsize(audio_path) / CHUNK_SIZE:.1f}초")
    print(f"  인식 언어     : {language}")
    print(f"  번역 모델     : {gemini_model}")
    print(f"  키워드부스팅  : 교회 용어 {len(CHURCH_KEYWORDS.split(','))}개")
    print("=" * 64)
    print(f"\n[STT Config]\n{json.dumps(json.loads(config_json), indent=2, ensure_ascii=False)}\n")
    print("-" * 64)
    print("  인식 + 번역 결과")
    print("-" * 64)

    # gRPC 채널 설정
    channel = grpc.secure_channel(GRPC_HOST, grpc.ssl_channel_credentials())
    stub = nest_pb2_grpc.NestServiceStub(channel)
    metadata = (("authorization", f"Bearer {secret_key}"),)

    results: list[TranslationResult] = []
    translation_tasks: list[asyncio.Task] = []
    start_time = time.time()

    async def process_translation(text, start_ts, end_ts, confidence, index):
        """개별 인식 결과에 대한 비동기 번역 처리"""
        tr_result = await translator.translate(text)
        tr_result.start_ts = start_ts
        tr_result.end_ts = end_ts
        tr_result.confidence = confidence

        # 번역 완료 시 즉시 출력
        timestamp = f"[{start_ts / 1000:.1f}s ~ {end_ts / 1000:.1f}s]"
        print(f"\n  #{index} {timestamp}  (신뢰도: {confidence:.3f})")
        print(f"  [KO] {text}")
        print(f"  [EN] {tr_result.translated}")
        if tr_result.error:
            print(f"  [⚠]  {tr_result.error}")
        print(f"  [⏱]  번역 지연: {tr_result.latency_ms:.0f}ms")

        return tr_result

    try:
        # gRPC 스트리밍은 동기이므로 별도 스레드에서 실행
        def run_grpc_stream():
            """gRPC 스트리밍을 동기적으로 실행하고 결과를 큐에 넣기"""
            stt_results = []
            try:
                responses = stub.recognize(
                    generate_requests(audio_path, config_json),
                    metadata=metadata,
                )
                for resp in responses:
                    data = json.loads(resp.contents)
                    stt_results.append(data)
            except grpc.RpcError as e:
                stt_results.append({"error": f"{e.code()} - {e.details()}"})
            return stt_results

        # gRPC를 별도 스레드에서 실행
        print("\n[*] STT 스트리밍 시작...\n")
        stt_results = await asyncio.to_thread(run_grpc_stream)

        # STT 결과를 순회하며 비동기 번역 태스크 생성
        seg_index = 0
        for data in stt_results:
            if "error" in data:
                print(f"\n[!] gRPC 오류: {data['error']}")
                continue

            response_type = data.get("responseType", [])

            # Config 응답 처리
            if "config" in response_type:
                status = data.get("config", {}).get("status", "")
                if status == "Success":
                    print(f"[✓] Config 설정 성공")
                    kb_status = data["config"].get("keywordBoosting", {}).get("status", "")
                    if kb_status:
                        print(f"    - 키워드부스팅: {kb_status}")
                else:
                    print(f"[✗] Config 실패: {status}")
                    break

            # 인식 결과 → 번역 태스크 생성
            if "transcription" in response_type:
                tr = data.get("transcription", {})
                text = tr.get("text", "")
                confidence = tr.get("confidence", 0)
                start_ts = tr.get("startTimestamp", 0)
                end_ts = tr.get("endTimestamp", 0)

                if text.strip():
                    seg_index += 1
                    task = asyncio.create_task(
                        process_translation(text, start_ts, end_ts, confidence, seg_index)
                    )
                    translation_tasks.append(task)

        # 모든 번역 태스크 완료 대기
        if translation_tasks:
            print(f"\n[*] {len(translation_tasks)}개 번역 태스크 병렬 처리 중...\n")
            completed = await asyncio.gather(*translation_tasks, return_exceptions=True)
            for item in completed:
                if isinstance(item, TranslationResult):
                    results.append(item)
                elif isinstance(item, Exception):
                    print(f"[!] 번역 태스크 예외: {item}")

    finally:
        channel.close()

    elapsed = time.time() - start_time

    # ──────────────────────────────────────────
    # 결과 요약
    # ──────────────────────────────────────────
    # 시간순 정렬
    results.sort(key=lambda r: r.start_ts)

    print("\n" + "=" * 64)
    print("  테스트 결과 요약")
    print("=" * 64)

    if results:
        avg_conf = sum(r.confidence for r in results) / len(results)
        avg_latency = sum(r.latency_ms for r in results) / len(results)
        max_latency = max(r.latency_ms for r in results)
        min_latency = min(r.latency_ms for r in results)
        error_count = sum(1 for r in results if r.error)

        full_ko = " ".join(r.original for r in results)
        full_en = " ".join(r.translated for r in results)

        print(f"  총 인식 구간      : {len(results)}개")
        print(f"  평균 STT 신뢰도   : {avg_conf:.4f}")
        print(f"  번역 오류         : {error_count}건")
        print()
        print(f"  ── Gemini 번역 성능 ──")
        print(f"  평균 번역 지연     : {avg_latency:.0f}ms")
        print(f"  최소 번역 지연     : {min_latency:.0f}ms")
        print(f"  최대 번역 지연     : {max_latency:.0f}ms")
        print(f"  전체 소요 시간     : {elapsed:.1f}초")
        print()

        # 실시간 자막 적합성 판단
        if avg_latency < 500:
            verdict = "✅ 우수 — 실시간 자막에 매우 적합"
        elif avg_latency < 1000:
            verdict = "🟡 양호 — 약간의 지연이 있으나 사용 가능"
        elif avg_latency < 2000:
            verdict = "🟠 보통 — 체감 가능한 지연, 개선 필요"
        else:
            verdict = "🔴 부적합 — 실시간 자막에 부적합"
        print(f"  실시간 적합성      : {verdict}")

        print(f"\n  ── 전체 한국어 텍스트 ──")
        for line in textwrap.wrap(full_ko, width=56):
            print(f"  {line}")

        print(f"\n  ── 전체 영어 번역 ──")
        for line in textwrap.wrap(full_en, width=56):
            print(f"  {line}")

        # JSON 결과 파일 저장
        output_path = os.path.splitext(audio_path)[0] + "_results.json"
        output_data = {
            "summary": {
                "total_segments": len(results),
                "avg_stt_confidence": round(avg_conf, 4),
                "avg_translation_latency_ms": round(avg_latency, 1),
                "min_translation_latency_ms": round(min_latency, 1),
                "max_translation_latency_ms": round(max_latency, 1),
                "total_elapsed_sec": round(elapsed, 1),
                "translation_errors": error_count,
                "gemini_model": translator.model,
            },
            "segments": [
                {
                    "index": i + 1,
                    "start_sec": r.start_ts / 1000,
                    "end_sec": r.end_ts / 1000,
                    "confidence": round(r.confidence, 4),
                    "ko": r.original,
                    "en": r.translated,
                    "translation_latency_ms": round(r.latency_ms, 1),
                    "error": r.error,
                }
                for i, r in enumerate(results)
            ],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\n  [📁] 상세 결과 저장: {output_path}")

    else:
        print("  인식 결과가 없습니다.")

    print("=" * 64)


# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CLOVA Speech STT + Gemini 번역 품질 테스트"
    )
    parser.add_argument(
        "--secret", required=True,
        help="CLOVA Speech 장문인식 Secret Key",
    )
    parser.add_argument(
        "--audio", required=True,
        help="PCM 음성 파일 경로 (16kHz, mono, 16bit)",
    )
    parser.add_argument(
        "--lang", default="ko",
        choices=["ko", "en", "ja"],
        help="인식 언어 (기본: ko)",
    )
    parser.add_argument(
        "--gemini-key", default=None,
        help="Gemini API Key (미지정 시 GEMINI_API_KEY 환경변수 사용)",
    )
    parser.add_argument(
        "--gemini-model", default="gemini-2.0-flash",
        help="Gemini 모델명 (기본: gemini-2.0-flash)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        print(f"[!] 파일을 찾을 수 없습니다: {args.audio}")
        sys.exit(1)

    ensure_proto_compiled()
    asyncio.run(run_test(
        args.secret, args.audio, args.lang,
        args.gemini_key, args.gemini_model,
    ))


if __name__ == "__main__":
    main()