#!/usr/bin/env python3
"""
CLOVA Speech 실시간 스트리밍 STT + 번역 품질 테스트
- 교회 자막/번역 서비스 도입을 위한 성능 평가용
- PCM 16kHz / mono / 16bit 음성 파일 기반 테스트

사전 준비:
  1. pip install grpcio grpcio-tools
  2. 테스트 음원을 PCM으로 변환:
     ffmpeg -i input.mp3 -ar 16000 -ac 1 -f s16le output.pcm
  3. CLOVA Speech Basic 장문인식 플랜 Secret Key 발급

사용법:
  python clova_stt_test.py --secret YOUR_SECRET_KEY --audio sermon_sample.pcm
"""

import argparse
import json
import os
import sys
import time
import textwrap

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
# 2. Config 생성 (STT + 번역 + 키워드부스팅 통합)
# ──────────────────────────────────────────────

# 교회 관련 키워드 부스팅 단어 목록
CHURCH_KEYWORDS = (
    "하나님,예수,그리스도,성령,아멘,할렐루야,"
    "말씀,은혜,찬양,기도,축복,구원,십자가,부활,"
    "성경,복음,믿음,소망,사랑,회개,세례,성찬,"
    "목사,장로,집사,권사,성도"
)


def build_config(language="ko", translate_to="en"):
    """통합 Config JSON 생성"""
    config = {
        # STT 언어 설정
        "transcription": {
            "language": language,
        },
        # 인식 결과 생성 기준
        "semanticEpd": {
            "skipEmptyText": True,        # 빈 결과 미전송
            "useWordEpd": True,           # 단어 단위 EPD
            "usePeriodEpd": True,         # 구두점 EPD (자막에 유리)
            "gapThreshold": 2000,         # 2초 묵음 시 결과 생성
            "durationThreshold": 20000,   # 최대 20초 단위 (설교 문장 길이 고려)
            "syllableThreshold": 0,
        },
        # 교회 용어 키워드 부스팅
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
# 3. gRPC 스트리밍 요청/응답 처리
# ──────────────────────────────────────────────

CHUNK_SIZE = 32000  # 32KB (1초 분량: 16kHz * 16bit * 1ch = 32000 bytes/sec)
GRPC_HOST = "clovaspeech-gw.ncloud.com:50051"


def generate_requests(audio_path, config_json):
    """Config 전송 후 PCM 청크를 순차 스트리밍"""
    import nest_pb2

    # (1) Config 전송
    yield nest_pb2.NestRequest(
        type=nest_pb2.RequestType.CONFIG,
        config=nest_pb2.NestConfig(config=config_json),
    )

    # (2) 오디오 청크 스트리밍
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
            # 실시간 스트리밍 시뮬레이션 (1초 분량씩 전송)
            time.sleep(len(chunk) / CHUNK_SIZE)

    print("\n[*] 오디오 전송 완료. 남은 응답 대기 중...\n")


def run_test(secret_key, audio_path, language, translate_to):
    """테스트 실행"""
    import grpc
    import nest_pb2_grpc

    config_json = build_config(language, translate_to)

    print("=" * 60)
    print("  CLOVA Speech STT + 번역 품질 테스트")
    print("=" * 60)
    print(f"  음성 파일 : {audio_path}")
    print(f"  파일 크기 : {os.path.getsize(audio_path):,} bytes")
    print(f"  예상 길이 : {os.path.getsize(audio_path) / CHUNK_SIZE:.1f}초")
    print(f"  인식 언어 : {language}")
    print(f"  번역 대상 : {translate_to}")
    print(f"  키워드부스팅: 교회 용어 {len(CHURCH_KEYWORDS.split(','))}개")
    print("=" * 60)
    print(f"\n[Config]\n{json.dumps(json.loads(config_json), indent=2, ensure_ascii=False)}\n")
    print("-" * 60)
    print("  인식 결과")
    print("-" * 60)

    # gRPC 채널 + 인증
    channel = grpc.secure_channel(GRPC_HOST, grpc.ssl_channel_credentials())
    stub = nest_pb2_grpc.NestServiceStub(channel)
    metadata = (("authorization", f"Bearer {secret_key}"),)

    results = []
    start_time = time.time()

    try:
        responses = stub.recognize(
            generate_requests(audio_path, config_json),
            metadata=metadata,
        )

        for resp in responses:
            data = json.loads(resp.contents)
            response_type = data.get("responseType", [])

            # Config 응답
            if "config" in response_type:
                status = data.get("config", {}).get("status", "")
                if status == "Success":
                    print(f"\n[✓] Config 설정 성공")
                    kb_status = data["config"].get("keywordBoosting", {}).get("status", "")
                    tr_status = data["config"].get("translation", {}).get("status", "") if "translation" in data.get("config", {}) else ""
                    if kb_status:
                        print(f"    - 키워드부스팅: {kb_status}")
                    if tr_status:
                        print(f"    - 번역: {tr_status}")
                    print()
                else:
                    print(f"\n[✗] Config 실패: {status}\n")
                    break

            # 인식 결과
            if "transcription" in response_type:
                tr = data.get("transcription", {})
                text = tr.get("text", "")
                confidence = tr.get("confidence", 0)
                start_ts = tr.get("startTimestamp", 0)
                end_ts = tr.get("endTimestamp", 0)
                epd_type = tr.get("epdType", "")

                timestamp = f"[{start_ts / 1000:.1f}s ~ {end_ts / 1000:.1f}s]"
                print(f"\n  {timestamp}  (신뢰도: {confidence:.3f}, EPD: {epd_type})")
                print(f"  [KO] {text}")

                results.append({
                    "text": text,
                    "confidence": confidence,
                    "start": start_ts,
                    "end": end_ts,
                    "epdType": epd_type,
                })

            # 번역 결과
            if "translation" in response_type:
                tl = data.get("translation", {})
                translated = tl.get("text", "")
                if translated:
                    print(f"  [EN] {translated}")

    except grpc.RpcError as e:
        print(f"\n[!] gRPC 오류: {e.code()} - {e.details()}")
    finally:
        channel.close()

    elapsed = time.time() - start_time

    # 결과 요약
    print("\n" + "=" * 60)
    print("  테스트 결과 요약")
    print("=" * 60)
    if results:
        avg_conf = sum(r["confidence"] for r in results) / len(results)
        full_text = "".join(r["text"] for r in results)
        print(f"  총 인식 구간  : {len(results)}개")
        print(f"  평균 신뢰도   : {avg_conf:.4f}")
        print(f"  소요 시간     : {elapsed:.1f}초")
        print(f"\n  [전체 텍스트]")
        for line in textwrap.wrap(full_text, width=50):
            print(f"  {line}")
    else:
        print("  인식 결과가 없습니다.")
    print("=" * 60)


# ──────────────────────────────────────────────
# 4. 메인
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CLOVA Speech 실시간 STT + 번역 품질 테스트"
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
        "--translate", default="en",
        help="번역 대상 언어 (기본: en)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        print(f"[!] 파일을 찾을 수 없습니다: {args.audio}")
        sys.exit(1)

    ensure_proto_compiled()
    run_test(args.secret, args.audio, args.lang, args.translate)


if __name__ == "__main__":
    main()