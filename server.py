import base64
import io
import json
import os
import uuid
import urllib.error
import urllib.request

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)

try:
    from pypdf import PdfReader
except ImportError:  # optional dependency, only needed for PDF uploads
    PdfReader = None

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.join(ROOT, "secrets.json")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "mimo-v2.5"
DEFAULT_CONTEXT_WINDOW = 1000000
DEFAULT_DOC_LIMIT = 8000
MAX_TOOL_ROUNDS = 8
FINDING_TYPES = ["APPROVED", "ERROR", "INFO"]

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
            "name": "add_finding",
            "description": (
                "Record one verification finding for a document. Call this once "
                "per finding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID from list_documents.",
                    },
                    "finding_type": {
                        "type": "string",
                        "enum": FINDING_TYPES,
                        "description": "APPROVED, ERROR, or INFO.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "What was found, with citations or quotes.",
                    },
                },
                "required": ["doc_id", "finding_type", "notes"],
            },
        },
    },
]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        return {}
    try:
        with open(SECRETS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_secrets(secrets):
    with open(SECRETS_PATH, "w", encoding="utf-8") as handle:
        json.dump(secrets, handle)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


CONFIG = load_config()
SECRETS = load_secrets()
FINDINGS = []

app = Flask(__name__)


def context_window():
    try:
        window = int(CONFIG.get("context_window", DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW
    return window if window > 0 else DEFAULT_CONTEXT_WINDOW


def list_documents(documents):
    return [
        {"id": doc_id, "name": doc["name"], "chars": len(doc["content"])}
        for doc_id, doc in documents.items()
    ]


def get_document(documents, doc_id, offset=0, limit=None):
    doc = documents.get(doc_id)
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


def add_finding(documents, doc_id, finding_type, notes):
    doc = documents.get(doc_id)
    if not doc:
        return {"error": f"document not found: {doc_id}"}
    if isinstance(finding_type, int):
        try:
            finding_type = FINDING_TYPES[finding_type]
        except IndexError:
            return {"error": f"finding_type index out of range: {finding_type}"}
    finding_type = str(finding_type or "").upper()
    if finding_type not in FINDING_TYPES:
        return {"error": f"finding_type must be one of {FINDING_TYPES}"}
    result = {
        "document_id": doc_id,
        "name": doc["name"],
        "type": finding_type,
        "notes": notes or "",
    }
    FINDINGS.append(result)
    return {"status": "recorded", "index": len(FINDINGS) - 1, **result}


def execute_tool(documents, name, arguments):
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid JSON arguments"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    if name == "list_documents":
        return {"documents": list_documents(documents)}
    if name == "get_document":
        return get_document(
            documents, args.get("id"), args.get("offset", 0), args.get("limit")
        )
    if name == "add_finding":
        return add_finding(
            documents,
            args.get("doc_id"),
            args.get("finding_type"),
            args.get("notes"),
        )
    return {"error": f"unknown tool: {name}"}


def accumulate_tool_calls(store, delta):
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


def _llm_request(messages, session_id):
    api_key = SECRETS.get("api_key")
    if not api_key:
        raise RuntimeError("No API key configured. Enter one in the browser.")
    base_url = CONFIG.get("base_url", DEFAULT_BASE_URL).rstrip("/")
    model = CONFIG.get("model", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request_obj = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "cq-assistant/1.0",
            "x-opencode-session": session_id or uuid.uuid4().hex,
        },
        method="POST",
    )
    return urllib.request.urlopen(request_obj, timeout=120)


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


def build_messages(history, has_documents):
    parts = []
    instructions = CONFIG.get("instructions")
    if instructions:
        parts.append(instructions.strip())
    if has_documents:
        parts.append(
            "Documents are available for this request. Use the list_documents "
            "tool to see them and the get_document tool to read their contents "
            "(paging with offset/limit as needed). Do not assume document "
            "contents. Record each result with add_finding."
        )
    messages = []
    if parts:
        messages.append({"role": "system", "content": "\n\n".join(parts)})
    messages.extend(history)
    return messages


def _sse(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def stream_chat(messages, session_id, upstream, documents):
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            tool_calls = {}
            assistant_text = ""
            with upstream:
                for event in iter_llm_stream(upstream):
                    if "usage" in event:
                        usage = event["usage"]
                        yield _sse(
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
                        yield _sse({"text": event["text"]})
                    elif "reasoning" in event:
                        yield _sse({"reasoning": event["reasoning"]})
                    elif "tool_call" in event:
                        accumulate_tool_calls(tool_calls, event["tool_call"])
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
            for call in calls:
                result = execute_tool(
                    documents,
                    call["function"]["name"],
                    call["function"]["arguments"],
                )
                yield _sse(
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
            try:
                upstream = _llm_request(messages, session_id)
            except urllib.error.HTTPError as error:
                yield _sse({"error": error_detail(error)})
                break
        yield "data: [DONE]\n\n"
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as error:  # noqa: BLE001
        yield _sse({"error": str(error)})


@app.get("/")
@app.get("/index.html")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/styles.css")
def styles():
    return send_from_directory(ROOT, "styles.css")


@app.get("/app.js")
def script():
    return send_from_directory(ROOT, "app.js")


@app.get("/api/config")
def api_config():
    return jsonify(
        {
            "model": CONFIG.get("model", DEFAULT_MODEL),
            "context_window": context_window(),
            "has_key": bool(SECRETS.get("api_key")),
        }
    )


@app.post("/api/key")
def api_set_key():
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    SECRETS["api_key"] = api_key
    save_secrets(SECRETS)
    return jsonify({"ok": True, "has_key": True})


@app.get("/api/findings")
def api_findings():
    return jsonify(FINDINGS)


@app.post("/api/extract")
def api_extract():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "untitled").strip()
    content = body.get("content") or ""
    if (body.get("encoding") or "text") == "base64":
        try:
            raw = base64.b64decode(content)
        except Exception as error:  # noqa: BLE001
            return jsonify({"error": f"Invalid base64 payload: {error}"}), 400
        if raw[:4] == b"%PDF" or name.lower().endswith(".pdf"):
            if PdfReader is None:
                return (
                    jsonify({"error": "pypdf is not installed (pip install pypdf)"}),
                    500,
                )
            try:
                content = extract_pdf_text(raw)
            except Exception as error:  # noqa: BLE001
                return jsonify({"error": f"Could not parse PDF: {error}"}), 400
        else:
            content = raw.decode("utf-8", errors="replace")
    return jsonify({"name": name, "content": content, "chars": len(content)})


@app.post("/api/chat")
def api_chat():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    documents = {}
    for item in body.get("documents", []):
        if not isinstance(item, dict):
            continue
        doc_id = item.get("id")
        content = item.get("content")
        if not doc_id or not isinstance(content, str):
            continue
        documents[doc_id] = {
            "name": item.get("name") or "untitled",
            "content": content,
        }
    messages = build_messages(body.get("messages", []), bool(documents))
    try:
        upstream = _llm_request(messages, session_id)
    except urllib.error.HTTPError as error:
        return jsonify({"error": error_detail(error)}), error.code
    except Exception as error:  # noqa: BLE001
        return jsonify({"error": str(error)}), 502
    return Response(
        stream_with_context(
            stream_chat(messages, session_id, upstream, documents)
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Serving on http://127.0.0.1:{port}  (model: {CONFIG.get('model', DEFAULT_MODEL)})")
    app.run(host="127.0.0.1", port=port, threaded=True)
