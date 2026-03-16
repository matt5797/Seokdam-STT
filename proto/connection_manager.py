"""
ConnectionManager — 다수 뷰어 WebSocket 관리 + 리플레이 버퍼
"""
import asyncio
from typing import Optional

from fastapi import WebSocket


class ConnectionManager:
    # 리플레이 버퍼 최대 크기 (세그먼트 × 번역 수를 고려)
    MAX_BUFFER = 24  # 8 세그먼트 × 3 (stt + 번역 2개)

    def __init__(self):
        self._admin: Optional[WebSocket] = None
        self._viewers: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._replay_buffer: list[dict] = []
        self._is_streaming: bool = False
        self._active_languages: list[str] = []
        self._audio_streaming: bool = False

    # ── 관리자 ──

    async def connect_admin(self, ws: WebSocket):
        async with self._lock:
            if self._admin is not None:
                # 기존 연결 교체 (중복 접속 시 기존 닫기)
                try:
                    await self._admin.close()
                except Exception:
                    pass
            self._admin = ws

    async def disconnect_admin(self, ws: WebSocket):
        async with self._lock:
            if self._admin is ws:
                self._admin = None

    # ── 뷰어 ──

    async def connect_viewer(self, ws: WebSocket):
        async with self._lock:
            self._viewers.add(ws)
        await self._send_viewer_count_to_admin()

    async def disconnect_viewer(self, ws: WebSocket):
        async with self._lock:
            self._viewers.discard(ws)
        await self._send_viewer_count_to_admin()

    async def send_current_state(self, ws: WebSocket):
        """신규 접속 뷰어에게 현재 상태 + 리플레이 버퍼 전송"""
        # 현재 스트리밍 상태 전송
        state_msg = {
            "type": "status",
            "state": "running" if self._is_streaming else "stopped",
            "message": "인식 중..." if self._is_streaming else "대기 중",
            "languages": self._active_languages,
        }
        try:
            await ws.send_json(state_msg)
        except Exception:
            return

        # 리플레이 버퍼 재전송
        for msg in list(self._replay_buffer):
            try:
                await ws.send_json(msg)
            except Exception:
                return

    async def broadcast_to_viewers(self, data: dict):
        """모든 뷰어에게 자막 브로드캐스트 + 죽은 연결 자동 정리"""
        self._update_replay_buffer(data)

        async with self._lock:
            viewers = set(self._viewers)

        dead: set[WebSocket] = set()
        for ws in viewers:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._viewers -= dead
            await self._send_viewer_count_to_admin()

    def set_audio_streaming(self, enabled: bool):
        """오디오 스트리밍 상태 설정"""
        self._audio_streaming = enabled

    @property
    def audio_streaming(self) -> bool:
        return self._audio_streaming

    async def broadcast_audio(self, data: bytes):
        """모든 뷰어에게 바이너리 오디오 프레임 브로드캐스트 (리플레이 없음)"""
        if not self._audio_streaming:
            return

        async with self._lock:
            viewers = set(self._viewers)

        if not viewers:
            return

        dead: set[WebSocket] = set()

        async def _send(ws):
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)

        await asyncio.gather(*(_send(ws) for ws in viewers))

        if dead:
            async with self._lock:
                self._viewers -= dead
            await self._send_viewer_count_to_admin()

    def set_streaming_state(self, is_streaming: bool, languages: list[str]):
        """스트리밍 상태 기록"""
        self._is_streaming = is_streaming
        self._active_languages = languages
        if not is_streaming:
            # 스트리밍 종료 시 버퍼 초기화
            self._replay_buffer.clear()

    def _update_replay_buffer(self, data: dict):
        """stt/translation 메시지만 버퍼링 (최대 MAX_BUFFER)"""
        if data.get("type") in ("stt", "translation"):
            self._replay_buffer.append(data)
            if len(self._replay_buffer) > self.MAX_BUFFER:
                self._replay_buffer.pop(0)

    async def _send_viewer_count_to_admin(self):
        """관리자에게 현재 뷰어 수 전송"""
        admin = self._admin
        if admin is None:
            return
        count = len(self._viewers)
        try:
            await admin.send_json({"type": "viewer_count", "count": count})
        except Exception:
            pass

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)
