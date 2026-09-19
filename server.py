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
DEFAULT_DOC_LIMIT = 8000
MAX_TOOL_ROUNDS = 8

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List the uploaded documents with their IDs, filenames and sizes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": (
                "Read the text of an uploaded document by ID. Use offset and "
                "limit to page through long documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Document ID from list_documents.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from (default 0).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 8000).",
                    },
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_verification",
            "description": "Record the final verification result and end the review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "Claim or requirement being verified.",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["pass", "fail", "needs_review"],
                        "description": "Verification outcome.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Supporting evidence with document citations.",
                    },
                },
                "required": ["claim", "verdict", "evidence"],
            },
        },
    },
]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json not found. Create it with api_key, base_url, model and instructions.")
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()
DOCUMENTS = {}
VERIFICATION_RESULTS = []


def context_window():
    try:
        window = int(CONFIG.get("context_window", DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    return window if window > 0 else DEFAULT_CONTEXT_WINDOW


def list_documents():
    return [
        {"id": doc_id, "name": doc["name"], "chars": len(doc["content"])}
        for doc_id, doc in DOCUMENTS.items()
    ]


def get_document(doc_id, offset=0, limit=None):
    doc = DOCUMENTS.get(doc_id)
    if not doc:
        return {"error": f"document not found: {doc_id}"}
    content = doc["content"]
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit) if limit else DEFAULT_DOC_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_DOC_LIMIT
    limit = max(1, limit)
    chunk = content[offset : offset + limit]
    return {
        "id": doc_id,
        "name": doc["name"],
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": len(content),
        "eof": offset + limit >= len(content),
        "content": chunk,
    }


def finish_verification(claim, verdict, evidence):
    result = {
        "claim": claim or "",
        "verdict": verdict or "",
        "evidence": evidence or "",
    }
    VERIFICATION_RESULTS.append(result)
    return {"status": "recorded", **result}


def execute_tool(name, arguments):
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid JSON arguments"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    if name == "list_documents":
        return {"documents": list_documents()}
    if name == "get_document":
        return get_document(
            args.get("id"), args.get("offset", 0), args.get("limit")
        )
    if name == "finish_verification":
        return finish_verification(
            args.get("claim"), args.get("verdict"), args.get("evidence")
        )
    return {"error": f"unknown tool: {name}"}


def accumulate_tool_calls(store, deltas):
    for delta in deltas:
        index = delta.get("index", 0)
        entry = store.setdefault(
            index, {"id": "", "name": "", "arguments": ""}
        )
        if delta.get("id"):
            entry["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]


def _llm_request(messages, session_id, stream):
    base_url = CONFIG.get("base_url", DEFAULT_BASE_URL).rstrip("/")
    model = CONFIG.get("model", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
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
    print(f"_llm_request called: {session_id}")
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
        delta = choices[0].get("delta", {})
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield {"reasoning": reasoning}
        text = delta.get("content")
        if text:
            yield {"text": text}
        for tool_call in delta.get("tool_calls") or []:
            yield {"tool_call": tool_call}


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
    if document_ids:
        parts.append(
            "Documents are available for this request. Use the list_documents "
            "tool to see them and the get_document tool to read their contents "
            "(paging with offset/limit as needed). Do not assume document "
            "contents. Call finish_verification once you reach a final verdict."
        )
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

    def _stream_response(self, messages, session_id, upstream):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                tool_calls = {}
                assistant_text = ""
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
                            assistant_text += event["text"]
                            self._write_event({"text": event["text"]})
                        elif "reasoning" in event:
                            self._write_event({"reasoning": event["reasoning"]})
                        elif "tool_call" in event:
                            accumulate_tool_calls(tool_calls, [event["tool_call"]])
                if not tool_calls:
                    break
                ordered = [tool_calls[index] for index in sorted(tool_calls)]
                calls = []
                for index, call in enumerate(ordered):
                    calls.append(
                        {
                            "id": call["id"] or f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"] or "{}",
                            },
                        }
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": calls,
                    }
                )
                finished = False
                for call in calls:
                    result = execute_tool(
                        call["function"]["name"], call["function"]["arguments"]
                    )
                    self._write_event(
                        {
                            "tool": {
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                                "result": result,
                            }
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result),
                        }
                    )
                    if call["function"]["name"] == "finish_verification":
                        finished = True
                        self._write_event(
                            {
                                "text": (
                                    "\n\n**Verification recorded**\n\n"
                                    f"- Claim: {result.get('claim', '')}\n"
                                    f"- Verdict: {result.get('verdict', '')}\n"
                                    f"- Evidence: {result.get('evidence', '')}\n"
                                )
                            }
                        )
                if finished:
                    break
                try:
                    upstream = _llm_request(messages, session_id, True)
                except urllib.error.HTTPError as error:
                    self._write_event({"error": error_detail(error)})
                    break
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
            session_id = body.get("session_id")
            messages = build_messages(
                body.get("messages", []), body.get("document_ids", [])
            )
            try:
                upstream = _llm_request(messages, session_id, True)
            except urllib.error.HTTPError as error:
                self._send_json(error.code, {"error": error_detail(error)})
                return
            except Exception as error:  # noqa: BLE001
                self._send_json(502, {"error": str(error)})
                return
            self._stream_response(messages, session_id, upstream)
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
