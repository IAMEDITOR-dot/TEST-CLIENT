#!/usr/bin/env python3
"""
Single-file, high-speed local web controller on port 8081.

Features (beta1):
- Prompts for URL on load.
- Renders target site in iframe.
- Shows live AI activity log.
- Ctrl+I toggles AI pause/resume and allows manual interaction.
- On resume, waits 3 seconds before continuing.

Run:
  python3 app.py
Open:
  http://127.0.0.1:8081
  http://127.0.0.1:8081/beta1
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8081
HOST = "0.0.0.0"


@dataclass
class RuntimeState:
    current_url: str = ""
    goal: str = ""
    ai_running: bool = False
    manual_mode: bool = False
    resume_at: float = 0.0
    step_count: int = 0
    actions_per_minute: int = 0
    last_minute_reset: float = field(default_factory=time.time)
    actions_in_current_minute: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)
    queued_user_hint: str = ""

    def push_log(self, kind: str, message: str) -> None:
        self.log.append({"t": time.time(), "kind": kind, "message": message})
        if len(self.log) > 400:
            del self.log[:150]


STATE = RuntimeState()
LOCK = threading.Lock()


def now() -> float:
    return time.time()


def ai_tick() -> None:
    """Lightweight fake agent loop for local UX/control testing."""
    canned_actions = [
        "Scanning viewport and identifying primary CTA.",
        "Comparing visible prices and sorting by lowest value signal.",
        "Checking for cookie/login wall and dismiss candidates.",
        "Reading heading hierarchy to stay aligned with goal.",
        "Searching for add-to-cart / next-level / play button.",
        "Waiting for dynamic content to settle before next move.",
    ]

    idx = 0
    while True:
        time.sleep(0.75)
        with LOCK:
            t = now()

            if t - STATE.last_minute_reset >= 60:
                STATE.actions_per_minute = STATE.actions_in_current_minute
                STATE.actions_in_current_minute = 0
                STATE.last_minute_reset = t

            if not STATE.ai_running:
                continue

            if STATE.manual_mode:
                continue

            if STATE.resume_at and t < STATE.resume_at:
                continue

            if not STATE.current_url:
                continue

            STATE.step_count += 1
            STATE.actions_in_current_minute += 1

            action_msg = canned_actions[idx % len(canned_actions)]
            idx += 1

            if STATE.queued_user_hint:
                action_msg = f"Applying your correction: {STATE.queued_user_hint}"
                STATE.queued_user_hint = ""

            STATE.push_log("ai", f"Step {STATE.step_count}: {action_msg}")


HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>Fast Local Web AI Controller</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0f14;
      --panel: #111924;
      --accent: #66e3ff;
      --text: #d8e3f0;
      --muted: #8fa4bb;
      --danger: #ff7070;
      --ok: #8dffb1;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      height: 100vh;
      display: grid;
      grid-template-columns: 1.7fr 1fr;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      overflow: hidden;
    }
    #left {
      display: grid;
      grid-template-rows: 42px 1fr;
      border-right: 1px solid #213043;
      min-width: 0;
    }
    #topbar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
      background: #0f1722;
      border-bottom: 1px solid #213043;
    }
    #status {
      font-size: 12px;
      color: var(--muted);
      margin-left: auto;
    }
    #frame-wrap {
      position: relative;
      width: 100%;
      height: 100%;
      background: #05080c;
    }
    iframe {
      width: 100%;
      height: 100%;
      border: 0;
      background: white;
    }
    #manual-overlay {
      position: absolute;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(11,15,20,0.42);
      color: #fff;
      font-size: 28px;
      font-weight: 700;
      pointer-events: none;
      text-shadow: 0 0 14px rgba(0,0,0,0.5);
    }
    #right {
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      min-width: 0;
      background: var(--panel);
    }
    .section {
      padding: 10px;
      border-bottom: 1px solid #1d2a3b;
    }
    h1 {
      margin: 0;
      font-size: 16px;
    }
    .row { display: flex; gap: 8px; }
    input, textarea, button {
      background: #0b121c;
      color: var(--text);
      border: 1px solid #2c3f57;
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 13px;
    }
    input, textarea { width: 100%; }
    textarea { resize: vertical; min-height: 64px; }
    button {
      cursor: pointer;
      white-space: nowrap;
      transition: transform 60ms ease, border-color 120ms ease;
    }
    button:hover { border-color: var(--accent); }
    button:active { transform: translateY(1px); }
    #log {
      overflow: auto;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      background: #0d1520;
    }
    .entry { margin-bottom: 8px; }
    .entry.ai { color: var(--ok); }
    .entry.user { color: var(--accent); }
    .entry.sys { color: var(--muted); }
    #hint {
      font-size: 11px;
      color: var(--muted);
      margin-top: 6px;
    }
    #prompt {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.55);
      display: grid;
      place-items: center;
      z-index: 20;
    }
    #prompt-card {
      width: min(740px, 90vw);
      background: #0f1722;
      border: 1px solid #2f4360;
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 25px 80px rgba(0,0,0,0.5);
    }
    #kbd {
      color: var(--danger);
      font-weight: 700;
    }
  </style>
</head>
<body>
  <div id="left">
    <div id="topbar">
      <button id="toggleBtn">Pause AI (Ctrl+I)</button>
      <button id="reloadBtn">Reload iframe</button>
      <div id="status">Idle</div>
    </div>
    <div id="frame-wrap">
      <iframe id="siteFrame" referrerpolicy="no-referrer"></iframe>
      <div id="manual-overlay">MANUAL MODE</div>
    </div>
  </div>

  <div id="right">
    <div class="section">
      <h1>AI Operator</h1>
      <div id="hint">Cross-origin pages render, but direct DOM/screenshot automation of iframe content needs remote browser tooling.</div>
    </div>

    <div class="section">
      <div class="row">
        <input id="goal" placeholder="Goal (e.g. Find lowest price under $50)" />
        <button id="setGoalBtn">Set Goal</button>
      </div>
      <div class="row" style="margin-top:8px;">
        <input id="message" placeholder="Ask AI / correction while paused" />
        <button id="sendMsgBtn">Send</button>
      </div>
      <div id="hint">Press <span id="kbd">Ctrl+I</span> to pause/resume. Resume waits 3s for re-orientation.</div>
    </div>

    <div id="log"></div>

    <div class="section" style="border-bottom:0;">
      <small id="metrics">step=0 | apm=0 | mode=auto</small>
    </div>
  </div>

  <div id="prompt">
    <div id="prompt-card">
      <h2 style="margin:0 0 10px 0;">Start Session</h2>
      <p style="margin:0 0 10px 0;color:#9eb4cc;">Enter a URL to load in the live viewport.</p>
      <div class="row">
        <input id="startUrl" placeholder="https://example.com" />
        <button id="startBtn">Load + Start AI</button>
      </div>
    </div>
  </div>

<script>
const $ = (s) => document.querySelector(s);
const statusEl = $('#status');
const logEl = $('#log');
const frameEl = $('#siteFrame');
const manualOverlay = $('#manual-overlay');
const toggleBtn = $('#toggleBtn');

function addLog(kind, msg) {
  const div = document.createElement('div');
  div.className = `entry ${kind}`;
  const t = new Date().toLocaleTimeString();
  div.textContent = `[${t}] ${msg}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

async function api(path, payload = null) {
  const opts = payload
    ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}
    : {method:'GET'};
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} failed`);
  return r.json();
}

async function refresh() {
  try {
    const st = await api('/api/state');
    statusEl.textContent = st.manual_mode
      ? `Paused/manual${st.resume_countdown ? ` | resume in ${st.resume_countdown}s` : ''}`
      : (st.ai_running ? 'AI running' : 'Idle');

    if (st.current_url && frameEl.src !== st.current_url) frameEl.src = st.current_url;

    manualOverlay.style.display = st.manual_mode ? 'flex' : 'none';
    toggleBtn.textContent = st.manual_mode ? 'Resume AI (Ctrl+I)' : 'Pause AI (Ctrl+I)';

    $('#metrics').textContent = `step=${st.step_count} | apm=${st.actions_per_minute} | mode=${st.manual_mode ? 'manual' : 'auto'}`;

    if (window.__lastLogCount !== st.log.length) {
      const newItems = st.log.slice(window.__lastLogCount || 0);
      for (const item of newItems) addLog(item.kind, item.message);
      window.__lastLogCount = st.log.length;
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
  }
}

async function toggleManual() {
  const st = await api('/api/toggle', {});
  if (st.manual_mode) {
    addLog('sys', 'Manual mode enabled. AI actions paused; you can interact directly.');
  } else {
    addLog('sys', 'Manual mode disabled. AI will resume after 3 seconds.');
  }
  await refresh();
}

$('#startBtn').addEventListener('click', async () => {
  const url = $('#startUrl').value.trim();
  if (!url) return;
  const goal = $('#goal').value.trim();
  const st = await api('/api/start', {url, goal});
  frameEl.src = st.current_url;
  $('#prompt').style.display = 'none';
  addLog('sys', `Session started for ${st.current_url}`);
});

$('#setGoalBtn').addEventListener('click', async () => {
  const msg = $('#goal').value.trim();
  if (!msg) return;
  await api('/api/message', {message: `[GOAL] ${msg}`});
  addLog('user', `Goal set: ${msg}`);
});

$('#sendMsgBtn').addEventListener('click', async () => {
  const msg = $('#message').value.trim();
  if (!msg) return;
  $('#message').value = '';
  await api('/api/message', {message: msg});
  addLog('user', msg);
});

$('#reloadBtn').addEventListener('click', () => {
  frameEl.src = frameEl.src;
  addLog('sys', 'Iframe reloaded.');
});

toggleBtn.addEventListener('click', toggleManual);

document.addEventListener('keydown', async (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === 'i') {
    e.preventDefault();
    await toggleManual();
  }
});

setInterval(refresh, 350);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict[str, Any], code: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        size = int(raw_len)
        data = self.rfile.read(size)
        return json.loads(data.decode("utf-8")) if data else {}

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html", "/beta1"):
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            with LOCK:
                countdown = 0
                if STATE.resume_at and now() < STATE.resume_at:
                    countdown = int(max(0.0, STATE.resume_at - now()) + 0.99)

                payload = {
                    "current_url": STATE.current_url,
                    "goal": STATE.goal,
                    "ai_running": STATE.ai_running,
                    "manual_mode": STATE.manual_mode,
                    "resume_countdown": countdown,
                    "step_count": STATE.step_count,
                    "actions_per_minute": STATE.actions_per_minute,
                    "log": STATE.log,
                }
            self._send_json(payload)
            return

        self._send_json({"error": "not found"}, code=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
        except Exception:
            self._send_json({"error": "invalid json"}, code=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/start":
            raw_url = str(payload.get("url", "")).strip()
            goal = str(payload.get("goal", "")).strip()
            if raw_url and not raw_url.startswith(("http://", "https://")):
                raw_url = "https://" + raw_url

            with LOCK:
                STATE.current_url = raw_url
                STATE.goal = goal
                STATE.ai_running = bool(raw_url)
                STATE.manual_mode = False
                STATE.resume_at = 0.0
                STATE.step_count = 0
                STATE.actions_in_current_minute = 0
                STATE.actions_per_minute = 0
                STATE.log.clear()
                STATE.push_log("sys", f"Target loaded: {raw_url}")
                if goal:
                    STATE.push_log("sys", f"Goal set: {goal}")

                result = {
                    "current_url": STATE.current_url,
                    "goal": STATE.goal,
                    "ai_running": STATE.ai_running,
                    "manual_mode": STATE.manual_mode,
                }
            self._send_json(result)
            return

        if self.path == "/api/toggle":
            with LOCK:
                STATE.manual_mode = not STATE.manual_mode
                if STATE.manual_mode:
                    STATE.push_log("sys", "Manual mode ON. Interact freely.")
                    STATE.resume_at = 0.0
                else:
                    STATE.resume_at = now() + 3.0
                    STATE.push_log("sys", "Manual mode OFF. Resuming in 3 seconds.")
                result = {
                    "manual_mode": STATE.manual_mode,
                    "resume_at": STATE.resume_at,
                }
            self._send_json(result)
            return

        if self.path == "/api/message":
            message = str(payload.get("message", "")).strip()
            if not message:
                self._send_json({"ok": False, "error": "empty message"}, code=HTTPStatus.BAD_REQUEST)
                return

            with LOCK:
                STATE.push_log("user", message)
                if message.startswith("[GOAL]"):
                    STATE.goal = message.replace("[GOAL]", "", 1).strip()
                    STATE.push_log("sys", f"Updated goal: {STATE.goal}")
                else:
                    STATE.queued_user_hint = message
                    STATE.push_log("sys", "Correction received and queued for next AI action.")
            self._send_json({"ok": True})
            return

        self._send_json({"error": "not found"}, code=HTTPStatus.NOT_FOUND)


def main() -> None:
    threading.Thread(target=ai_tick, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Fast Local Web AI Controller listening on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
