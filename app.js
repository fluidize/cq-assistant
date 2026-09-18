const fileInput = document.getElementById("file-input");
const documentList = document.getElementById("document-list");
const chatArea = document.getElementById("chat-area");
const promptForm = document.getElementById("prompt-form");
const promptInput = document.getElementById("prompt-input");

const documents = [];
const history = [];
const sessionId =
  window.crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : "s-" + Date.now().toString(36) + Math.random().toString(36).slice(2);

if (location.protocol === "file:") {
  showError(
    "This page is open as a file:// URL, so the backend is unreachable. " +
      "Run `python3 server.py` and open http://127.0.0.1:8000 instead."
  );
} else {
  loadDocuments();
}

async function apiFetch(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text.trim() || response.statusText };
  }
  if (!response.ok) {
    throw new Error(data.error || response.status + " " + response.statusText);
  }
  return data;
}

fileInput.addEventListener("change", async () => {
  for (const file of fileInput.files) {
    try {
      const content = await readFile(file);
      const doc = await apiFetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: file.name, content }),
      });
      documents.push(doc);
      renderDocuments();
    } catch (error) {
      showError("Upload failed for " + file.name + ": " + error.message);
    }
  }
  fileInput.value = "";
});

function readFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => resolve("");
    reader.readAsText(file);
  });
}

async function loadDocuments() {
  try {
    const list = await apiFetch("/api/documents");
    documents.length = 0;
    documents.push(...list);
    renderDocuments();
  } catch (error) {
    showError("Could not load documents: " + error.message);
  }
}

function renderDocuments() {
  documentList.innerHTML = "";
  documents.forEach((doc) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = doc.name;
    const remove = document.createElement("button");
    remove.className = "remove";
    remove.setAttribute("aria-label", "Remove " + doc.name);
    remove.innerHTML =
      '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">' +
      '<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round"/></svg>';
    remove.addEventListener("click", async () => {
      try {
        await apiFetch("/api/documents/" + doc.id, { method: "DELETE" });
        documents.splice(documents.indexOf(doc), 1);
        renderDocuments();
      } catch (error) {
        showError("Could not remove " + doc.name + ": " + error.message);
      }
    });
    li.append(name, remove);
    documentList.append(li);
  });
}

promptForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text) return;

  appendMessage("user", text);
  history.push({ role: "user", content: text });
  promptInput.value = "";
  promptInput.style.height = "auto";

  const loading = appendLoading();
  let bubble = null;
  let reply = "";

  const onText = (chunk) => {
    if (!bubble) {
      loading.remove();
      bubble = appendMessage("assistant", "");
    }
    reply += chunk;
    renderStreamChunk(bubble, chunk);
  };

  try {
    await streamChat(onText);
  } catch (error) {
    const message = "Error: " + error.message;
    if (bubble) {
      renderStreamChunk(bubble, "\n\n" + message);
    } else {
      loading.remove();
      appendMessage("assistant", message);
    }
  }
  loading.remove();
  if (reply) {
    history.push({ role: "assistant", content: reply });
  }
});

async function streamChat(onText) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: history,
      document_ids: documents.map((doc) => doc.id),
      session_id: sessionId,
    }),
  });

  if (!response.ok) {
    const raw = await response.text();
    let message = raw;
    try {
      message = JSON.parse(raw).error || raw;
    } catch {
      /* keep raw */
    }
    throw new Error(message || response.status + " " + response.statusText);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop();
    for (const event of events) {
      const line = event.trim();
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (parsed.error) throw new Error(parsed.error);
      if (parsed.text) onText(parsed.text);
    }
  }
}

function renderStreamChunk(container, chunk) {
  const state = container._stream || (container._stream = { open: null });
  const parts = chunk.split(/(\s+)/);

  for (const token of parts) {
    if (token === "") continue;
    if (/^\s+$/.test(token)) {
      state.open = null;
      container.append(document.createTextNode(token));
    } else {
      if (!state.open) {
        state.open = wordSpan("");
        container.append(state.open);
      }
      state.open.textContent += token;
    }
  }

  chatArea.scrollTop = chatArea.scrollHeight;
}

function wordSpan(text) {
  const span = document.createElement("span");
  span.className = "word";
  span.textContent = text;
  return span;
}

function showError(message) {
  console.error(message);
  appendMessage("assistant", message);
}

promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = promptInput.scrollHeight + "px";
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    promptForm.requestSubmit();
  }
});

function appendMessage(role, text) {
  const empty = chatArea.querySelector(".empty");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "message " + role;
  div.textContent = text;
  chatArea.append(div);
  chatArea.scrollTop = chatArea.scrollHeight;
  return div;
}

function appendLoading() {
  const empty = chatArea.querySelector(".empty");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "message assistant loading";
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("span");
    dot.className = "dot";
    div.append(dot);
  }
  chatArea.append(div);
  chatArea.scrollTop = chatArea.scrollHeight;
  return div;
}
