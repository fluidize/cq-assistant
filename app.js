const fileInput = document.getElementById("file-input");
const documentList = document.getElementById("document-list");
const chatScroll = document.getElementById("chat-scroll");
const ctxUsage = document.getElementById("ctx-usage");
const promptForm = document.getElementById("prompt-form");
const promptInput = document.getElementById("prompt-input");
const resetBtn = document.getElementById("reset-btn");

const HISTORY_KEY = "cq.history";
const SESSION_KEY = "cq.sessionId";
const USAGE_KEY = "cq.usedTokens";

const documents = [];
const history = loadHistory();
let sessionId = loadSessionId();
let usedTokens = loadUsedTokens();
let contextWindow = null;

function loadHistory() {
  try {
    const stored = JSON.parse(localStorage.getItem(HISTORY_KEY));
    return Array.isArray(stored) ? stored : [];
  } catch {
    return [];
  }
}

function newSessionId() {
  return window.crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : "s-" + Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function loadSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = newSessionId();
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function saveChat() {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
}

function loadUsedTokens() {
  const value = Number(localStorage.getItem(USAGE_KEY));
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function formatTokens(value) {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1000000) {
    const millions = value / 1000000;
    return (Number.isInteger(millions) ? millions : millions.toFixed(1)) + "M";
  }
  if (value >= 1000) {
    const thousands = value / 1000;
    return (Number.isInteger(thousands) ? thousands : thousands.toFixed(1)) + "K";
  }
  return String(value);
}

function updateCtxUsage() {
  if (!ctxUsage) return;
  const total = contextWindow ? formatTokens(contextWindow) : "—";
  ctxUsage.textContent = formatTokens(usedTokens) + " / " + total + " tokens";
}

async function loadConfig() {
  try {
    const data = await apiFetch("/api/config");
    contextWindow = data.context_window || null;
  } catch {
    contextWindow = null;
  }
  updateCtxUsage();
}

let stick = true;

chatScroll.addEventListener("scroll", () => {
  stick =
    chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 40;
});

function scrollToBottom() {
  if (stick) {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }
}

if (typeof marked !== "undefined") {
  marked.setOptions({ gfm: true, breaks: true });
}

if (location.protocol === "file:") {
  showError(
    "This page is open as a file:// URL, so the backend is unreachable. " +
      "Run `python3 server.py` and open http://127.0.0.1:8000 instead."
  );
} else {
  updateCtxUsage();
  loadConfig();
  loadDocuments();
  renderHistory();
}

function renderMarkdown(container, text) {
  const target = container.querySelector(".message-body") || container;
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    target.textContent = text;
    scrollToBottom();
    return;
  }
  target.innerHTML = DOMPurify.sanitize(marked.parse(text));
  target.querySelectorAll("a").forEach((link) => {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
  scrollToBottom();
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
      const isPdf =
        file.type === "application/pdf" || /\.pdf$/i.test(file.name);
      const content = isPdf
        ? await readFileBase64(file)
        : await readFile(file);
      const doc = await apiFetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: file.name,
          content,
          encoding: isPdf ? "base64" : "text",
        }),
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

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.onerror = () => reject(new Error("Could not read file"));
    reader.readAsDataURL(file);
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

  stick = true;
  appendMessage("user", text);
  history.push({ role: "user", content: text });
  saveChat();
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
    renderMarkdown(bubble, reply);
  };

  try {
    await streamChat(onText);
  } catch (error) {
    const message = "Error: " + error.message;
    if (bubble) {
      reply += "\n\n" + message;
      renderMarkdown(bubble, reply);
    } else {
      loading.remove();
      appendMessage("assistant", message);
    }
  }
  loading.remove();
  if (reply) {
    history.push({ role: "assistant", content: reply });
    saveChat();
  }
});

function applyUsage(usage) {
  if (typeof usage.context_window === "number") {
    contextWindow = usage.context_window;
  }
  if (typeof usage.total_tokens === "number") {
    usedTokens = usage.total_tokens;
    localStorage.setItem(USAGE_KEY, String(usedTokens));
  }
  updateCtxUsage();
}

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
      if (parsed.usage) applyUsage(parsed.usage);
      if (parsed.text) onText(parsed.text);
    }
  }
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

function renderHistory() {
  history.forEach((message) => appendMessage(message.role, message.content));
}

resetBtn.addEventListener("click", () => {
  if (!confirm("Start a new chat? This clears the current conversation.")) return;
  history.length = 0;
  localStorage.removeItem(HISTORY_KEY);
  sessionId = newSessionId();
  localStorage.setItem(SESSION_KEY, sessionId);
  chatScroll.innerHTML = '<p class="empty">Start the conversation</p>';
  usedTokens = 0;
  localStorage.removeItem(USAGE_KEY);
  updateCtxUsage();
  stick = true;
});

function appendMessage(role, text) {
  const empty = chatScroll.querySelector(".empty");
  if (empty) empty.remove();

  const div = createMessage(role);
  if (role === "assistant") {
    renderMarkdown(div, text);
  } else {
    div.querySelector(".message-body").textContent = text;
  }
  chatScroll.append(div);
  scrollToBottom();
  return div;
}

function createMessage(role) {
  const div = document.createElement("div");
  div.className = "message " + role;

  const body = document.createElement("div");
  body.className = "message-body";

  div.append(body);
  return div;
}

function appendLoading() {
  const empty = chatScroll.querySelector(".empty");
  if (empty) empty.remove();

  const div = createMessage("assistant");
  div.classList.add("loading");
  const body = div.querySelector(".message-body");
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("span");
    dot.className = "dot";
    body.append(dot);
  }
  chatScroll.append(div);
  scrollToBottom();
  return div;
}
