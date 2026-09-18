import base64
import io
import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from pypdf import PdfReader
except ImportError:  # optional dependency, only needed for PDF uploads
    PdfReader = None

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "mimo-v2.5"
DEFAULT_CONTEXT_WINDOW = 1000000
MAX_DOC_CHARS = 20000

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json not found. Create it with api_key, base_url, model and instructions.")
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()
DOCUMENTS = {}


def context_window():
    try:
        window = int(CONFIG.get("context_window", DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    return window if window > 0 else DEFAULT_CONTEXT_WINDOW


def _llm_request(messages, session_id, stream):
    base_url = CONFIG.get("base_url", DEFAULT_BASE_URL).rstrip("/")
    model = CONFIG.get("model", DEFAULT_MODEL)
    payload = {"model": model, "messages": messages}
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + CONFIG["api_key"],
            "User-Agent": "cq-assistant/1.0",
            "x-opencode-session": session_id or uuid.uuid4().hex,
        },
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=120)


def iter_llm_stream(response):
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if usage:
            yield {"usage": usage}
        choices = chunk.get("choices") or []
        if not choices:
            continue
        text = choices[0].get("delta", {}).get("content")
        if text:
            yield {"text": text}


def error_detail(error):
    detail = error.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(detail).get("error")
    except (json.JSONDecodeError, AttributeError):
        return detail.strip()
    if isinstance(parsed, dict):
        return parsed.get("message", detail).strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return detail.strip()


def extract_pdf_text(data):
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def build_messages(history, document_ids):
    parts = []
    instructions = CONFIG.get("instructions")
    if instructions:
        parts.append(instructions.strip())
    context = []
    for doc_id in document_ids:
        doc = DOCUMENTS.get(doc_id)
        if not doc:
            continue
        context.append(f"--- {doc['name']} ---\n{doc['content'][:MAX_DOC_CHARS]}")
    if context:
        parts.append("Use the following documents as context:\n\n" + "\n\n".join(context))
    messages = []
    if parts:
        messages.append({"role": "system", "content": "\n\n".join(parts)})
    messages.extend(history)
    return messages


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_response(self, upstream):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with upstream:
                for event in iter_llm_stream(upstream):
                    if "usage" in event:
                        usage = event["usage"]
                        self._write_event(
                            {
                                "usage": {
                                    "prompt_tokens": usage.get("prompt_tokens"),
                                    "completion_tokens": usage.get("completion_tokens"),
                                    "total_tokens": usage.get("total_tokens"),
                                    "context_window": context_window(),
                                }
                            }
                        )
                    elif "text" in event:
                        self._write_event({"text": event["text"]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001
            try:
                self._write_event({"error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _write_event(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _serve_static(self, path):
        filename = STATIC_FILES.get(path)
        if not filename:
            self._send_json(404, {"error": "not found"})
            return
        filepath = os.path.join(ROOT, filename)
        with open(filepath, "rb") as handle:
            data = handle.read()
        content_type = "text/html" if filename.endswith(".html") else (
            "text/css" if filename.endswith(".css") else "text/javascript"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in STATIC_FILES:
            self._serve_static(self.path)
        elif self.path == "/api/config":
            self._send_json(
                200,
                {
                    "model": CONFIG.get("model", DEFAULT_MODEL),
                    "context_window": context_window(),
                },
            )
        elif self.path == "/api/documents":
            self._send_json(
                200,
                [
                    {"id": doc_id, "name": doc["name"], "chars": len(doc["content"])}
                    for doc_id, doc in DOCUMENTS.items()
                ],
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/documents":
            body = self._read_json()
            name = (body.get("name") or "untitled").strip()
            content = body.get("content") or ""
            if (body.get("encoding") or "text") == "base64":
                try:
                    raw = base64.b64decode(content)
                except Exception as error:  # noqa: BLE001
                    self._send_json(400, {"error": f"Invalid base64 payload: {error}"})
                    return
                if raw[:4] == b"%PDF" or name.lower().endswith(".pdf"):
                    if PdfReader is None:
                        self._send_json(
                            500, {"error": "pypdf is not installed (pip install pypdf)"}
                        )
                        return
                    try:
                        content = extract_pdf_text(raw)
                    except Exception as error:  # noqa: BLE001
                        self._send_json(400, {"error": f"Could not parse PDF: {error}"})
                        return
                else:
                    content = raw.decode("utf-8", errors="replace")
            doc_id = uuid.uuid4().hex
            DOCUMENTS[doc_id] = {"name": name, "content": content}
            self._send_json(201, {"id": doc_id, "name": name, "chars": len(content)})
        elif self.path == "/api/chat":
            body = self._read_json()
            messages = build_messages(
                body.get("messages", []), body.get("document_ids", [])
            )
            try:
                upstream = _llm_request(messages, body.get("session_id"), True)
            except urllib.error.HTTPError as error:
                self._send_json(error.code, {"error": error_detail(error)})
                return
            except Exception as error:  # noqa: BLE001
                self._send_json(502, {"error": str(error)})
                return
            self._stream_response(upstream)
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/documents/"):
            doc_id = self.path.rsplit("/", 1)[-1]
            if DOCUMENTS.pop(doc_id, None):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "not found"})
        else:
            self._send_json(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving on http://127.0.0.1:{port}  (model: {CONFIG.get('model', DEFAULT_MODEL)})")
    server.serve_forever()
