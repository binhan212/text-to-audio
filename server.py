import asyncio
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import edge_tts


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8000
MAX_TEXT_LENGTH = 100_000
CHUNK_LENGTH = 1_000
MAX_CONCURRENT_REQUESTS = 6
VIETNAMESE_VOICES = {
    "vi-VN-HoaiMyNeural": "Hoài My — Nữ",
    "vi-VN-NamMinhNeural": "Nam Minh — Nam",
}


def percentage(value: float, neutral: float, scale: float = 100) -> str:
    amount = round((value - neutral) * scale)
    return f"{amount:+d}%"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "VietnameseTTS/1.0"

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/voices":
            self.send_json(
                200,
                {
                    "voices": [
                        {"id": voice_id, "name": name}
                        for voice_id, name in VIETNAMESE_VOICES.items()
                    ]
                },
            )
            return

        relative_path = "index.html" if path == "/" else path.lstrip("/")
        requested_file = (ROOT / relative_path).resolve()
        if ROOT not in requested_file.parents and requested_file != ROOT:
            self.send_error(403)
            return
        if not requested_file.is_file():
            self.send_error(404)
            return

        content = requested_file.read_bytes()
        content_type, _ = mimetypes.guess_type(requested_file.name)
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/tts":
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 1_000_000:
                self.send_json(413, {"error": "Dữ liệu gửi lên quá lớn."})
                return
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            text = str(payload.get("text", "")).strip()
            voice = str(payload.get("voice", "vi-VN-HoaiMyNeural"))
            rate = float(payload.get("rate", 1))
            pitch = float(payload.get("pitch", 1))
            volume = float(payload.get("volume", 1))
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Dữ liệu không hợp lệ."})
            return

        if not text:
            self.send_json(400, {"error": "Vui lòng nhập nội dung cần đọc."})
            return
        if len(text) > MAX_TEXT_LENGTH:
            self.send_json(400, {"error": f"Nội dung tối đa {MAX_TEXT_LENGTH:,} ký tự."})
            return
        if voice not in VIETNAMESE_VOICES:
            self.send_json(400, {"error": "Giọng đọc không hợp lệ."})
            return
        if not (0.5 <= rate <= 2 and 0.5 <= pitch <= 2 and 0 <= volume <= 1):
            self.send_json(400, {"error": "Thông số giọng đọc không hợp lệ."})
            return

        try:
            audio = asyncio.run(
                self.synthesize(
                    text,
                    voice,
                    percentage(rate, 1),
                    f"{round((pitch - 1) * 50):+d}Hz",
                    percentage(volume, 1),
                )
            )
        except Exception as error:
            print(f"TTS error: {error}")
            self.send_json(
                502,
                {"error": "Không thể kết nối dịch vụ giọng đọc. Hãy kiểm tra Internet và thử lại."},
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Disposition", 'inline; filename="giong-doc-tieng-viet.mp3"')
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    @staticmethod
    async def synthesize(text: str, voice: str, rate: str, pitch: str, volume: str) -> bytes:
        text_parts = AppHandler.split_text(text)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def render_part(part: str) -> bytes:
            async with semaphore:
                for attempt in range(3):
                    try:
                        audio_chunks = []
                        communicate = edge_tts.Communicate(
                            text=part,
                            voice=voice,
                            rate=rate,
                            pitch=pitch,
                            volume=volume,
                            connect_timeout=15,
                            receive_timeout=90,
                        )
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":
                                audio_chunks.append(chunk["data"])
                        if audio_chunks:
                            return b"".join(audio_chunks)
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(attempt + 1)
                raise RuntimeError("No audio returned")

        rendered_parts = await asyncio.gather(*(render_part(part) for part in text_parts))
        return b"".join(rendered_parts)

    @staticmethod
    def split_text(text: str) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
        parts = []
        current = ""

        for paragraph in paragraphs:
            sentences = re.split(r"(?<=[.!?…])\s+", paragraph)
            for sentence in sentences:
                while len(sentence) > CHUNK_LENGTH:
                    split_at = sentence.rfind(" ", 0, CHUNK_LENGTH)
                    if split_at < CHUNK_LENGTH // 2:
                        split_at = CHUNK_LENGTH
                    piece, sentence = sentence[:split_at], sentence[split_at:].lstrip()
                    if current:
                        parts.append(current)
                        current = ""
                    parts.append(piece)

                candidate = f"{current} {sentence}".strip()
                if len(candidate) <= CHUNK_LENGTH:
                    current = candidate
                else:
                    if current:
                        parts.append(current)
                    current = sentence

        if current:
            parts.append(current)
        return parts or [text]


if __name__ == "__main__":
    print(f"Ứng dụng đang chạy tại http://{HOST}:{PORT}")
    print("Nhấn Ctrl+C để dừng.")
    ThreadingHTTPServer((HOST, PORT), AppHandler).serve_forever()
