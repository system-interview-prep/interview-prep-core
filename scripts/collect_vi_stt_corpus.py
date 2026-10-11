"""Local browser recorder for the consented Vietnamese STT corpus."""

# The embedded single-file browser client intentionally keeps its HTML/JS
# compact so the collector has no build/runtime dependency.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = r"""<!doctype html><html lang="vi"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thu corpus STT tiếng Việt</title>
<style>body{font:16px system-ui;max-width:760px;margin:32px auto;padding:0 16px;color:#15244b}
label{display:block;margin:12px 0 4px}input,select,textarea,button{font:inherit;padding:10px;width:100%;box-sizing:border-box}
button{margin-top:12px;background:#294fa6;color:white;border:0;border-radius:8px}button:disabled{opacity:.45}
.prompt{padding:18px;background:#eef3ff;border-radius:12px;font-size:20px;margin:20px 0}.status{min-height:24px}</style>
<h1>Thu corpus STT tiếng Việt</h1>
<p>Chỉ ghi âm khi người tham gia đã đọc và đồng ý nội dung consent nội bộ.</p>
<label>Slot ID<input id="slot" placeholder="north-speaker-01"></label>
<label>Mã người nói (bút danh, không dùng họ tên)<input id="speaker"></label>
<label><input id="consent" type="checkbox" style="width:auto"> Tôi đồng ý dùng bản ghi cho đánh giá STT nội bộ.</label>
<button id="load">Tải câu tiếp theo</button><div id="prompt" class="prompt">Chưa tải câu hỏi.</div>
<button id="record" disabled>Bắt đầu ghi</button><button id="stop" disabled>Dừng ghi</button>
<label>Ground truth — chép đúng những gì đã nói<textarea id="reference" rows="4"></textarea></label>
<button id="save" disabled>Lưu mẫu</button><p id="status" class="status"></p>
<script>
let sample=null, recorder=null, chunks=[], blob=null, started=0;
const $=id=>document.getElementById(id); const status=t=>$('status').textContent=t;
$('load').onclick=async()=>{if(!$('consent').checked||!$('speaker').value.trim())return status('Cần consent và mã người nói.');
 const r=await fetch('/api/next?slotId='+encodeURIComponent($('slot').value.trim()));
 if(!r.ok)return status(await r.text()); sample=await r.json(); $('prompt').textContent=sample.prompt; $('record').disabled=false; status('Sẵn sàng. Hãy trả lời tự nhiên, không đọc câu hỏi.');};
$('record').onclick=async()=>{const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
 chunks=[]; recorder=new MediaRecorder(stream); recorder.ondataavailable=e=>chunks.push(e.data); recorder.start(); started=performance.now();
 $('record').disabled=true;$('stop').disabled=false;status('Đang ghi…');};
$('stop').onclick=()=>{recorder.onstop=()=>{blob=new Blob(chunks,{type:recorder.mimeType}); recorder.stream.getTracks().forEach(t=>t.stop()); $('save').disabled=false;status('Đã dừng. Hãy chép ground truth rồi lưu.');};recorder.stop();$('stop').disabled=true;};
$('save').onclick=async()=>{const reference=$('reference').value.trim();if(!reference)return status('Ground truth không được để trống.');
 const data=await new Promise(ok=>{const r=new FileReader();r.onload=()=>ok(r.result.split(',')[1]);r.readAsDataURL(blob)});
 const r=await fetch('/api/sample',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id:sample.id,speakerId:$('speaker').value.trim(),reference,consent:'internal-eval-consent-v1',durationSeconds:(performance.now()-started)/1000,mimeType:blob.type,audioBase64:data})});
 if(!r.ok)return status(await r.text()); $('reference').value='';blob=null;$('save').disabled=true;status('Đã lưu. Đang tải câu tiếp theo…');$('load').click();};
</script></html>"""


class CorpusStore:
    def __init__(self, root: Path, plan_path: Path) -> None:
        self.root = root.resolve()
        self.audio_dir = self.root / "audio" / "test"
        self.manifest_path = self.root / "manifest.jsonl"
        self.plan = [json.loads(line) for line in plan_path.read_text(encoding="utf-8").splitlines() if line]
        self.plan_by_id = {row["id"]: row for row in self.plan}
        self.lock = threading.Lock()

    def completed(self) -> dict[str, dict]:
        if not self.manifest_path.exists():
            return {}
        rows = [json.loads(line) for line in self.manifest_path.read_text(encoding="utf-8").splitlines() if line]
        return {row["id"]: row for row in rows}

    def next(self, slot_id: str) -> dict | None:
        done = self.completed()
        return next((row for row in self.plan if row["slotId"] == slot_id and row["id"] not in done), None)

    def save(self, payload: dict) -> dict:
        sample_id = str(payload.get("id", ""))
        plan = self.plan_by_id.get(sample_id)
        if plan is None:
            raise ValueError("sample id is not in the frozen collection plan")
        if payload.get("consent") != "internal-eval-consent-v1":
            raise ValueError("valid consent is required")
        speaker_id = str(payload.get("speakerId", "")).strip()
        reference = str(payload.get("reference", "")).strip()
        if not speaker_id or not reference:
            raise ValueError("speakerId and human ground truth are required")
        raw = base64.b64decode(str(payload.get("audioBase64", "")), validate=True)
        if len(raw) < 1024:
            raise ValueError("audio recording is empty or too short")
        extension = mimetypes.guess_extension(str(payload.get("mimeType", "")).split(";", 1)[0]) or ".webm"
        if extension in {".weba", ".oga"}:
            extension = ".webm" if extension == ".weba" else ".ogg"
        relative_path = Path("audio") / "test" / f"{sample_id}{extension}"
        destination = self.root / relative_path
        row = {
            **plan,
            "audioPath": relative_path.as_posix(),
            "reference": reference,
            "speakerId": speaker_id,
            "durationSeconds": round(float(payload.get("durationSeconds", 0)), 3),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "consent": payload["consent"],
            "mimeType": str(payload.get("mimeType", "application/octet-stream")),
        }
        with self.lock:
            existing = self.completed()
            if sample_id in existing:
                raise ValueError("sample was already recorded")
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            existing[sample_id] = row
            ordered = [existing[key] for key in sorted(existing)]
            self.manifest_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered),
                encoding="utf-8",
            )
        return {"saved": sample_id, "completed": len(existing), "target": len(self.plan)}


def handler(store: CorpusStore):
    class Handler(BaseHTTPRequestHandler):
        def send_value(self, status: HTTPStatus, value: object, content_type: str = "application/json") -> None:
            data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", f"{content_type}; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_value(HTTPStatus.OK, PAGE, "text/html")
                return
            if parsed.path == "/api/next":
                slot = parse_qs(parsed.query).get("slotId", [""])[0]
                sample = store.next(slot)
                self.send_value(HTTPStatus.OK if sample else HTTPStatus.NOT_FOUND, sample or "Slot đã đủ 10 mẫu hoặc không tồn tại.")
                return
            self.send_value(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:
            if self.path != "/api/sample":
                self.send_value(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                size = int(self.headers.get("content-length", "0"))
                if size > 25_000_000:
                    raise ValueError("recording exceeds 25 MB")
                payload = json.loads(self.rfile.read(size))
                self.send_value(HTTPStatus.CREATED, store.save(payload))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_value(HTTPStatus.BAD_REQUEST, str(exc))

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    store = CorpusStore(args.root, args.plan)
    server = ThreadingHTTPServer((args.host, args.port), handler(store))
    print(f"collector: http://{args.host}:{args.port} ({len(store.completed())}/{len(store.plan)} complete)")
    server.serve_forever()


if __name__ == "__main__":
    main()
