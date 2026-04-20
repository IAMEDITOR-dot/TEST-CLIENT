# Beta1 (Codespaces Quick Start)

This repo runs a local single-file web app (`app.py`) on port **8081**.

## Start in GitHub Codespaces

1. Open this repo in a Codespace.
2. In the terminal, run:

   ```bash
   python3 app.py
   ```

3. Wait for this log line:

   ```text
   Fast Local Web AI Controller listening on http://127.0.0.1:8081
   ```

## Open port 8081 in a new tab

### Option A: From the terminal pop-up
- When Codespaces detects port 8081, click **Open in Browser**.

### Option B: From the Ports panel
1. Open **Ports** in the bottom panel.
2. Find port **8081** (or add it if needed).
3. Right-click it and choose **Open in Browser** (new tab).

## Start beta1 UI

Open either URL in your browser tab:
- `/` -> `https://<your-codespace-url>-8081.app.github.dev/`
- `/beta1` -> `https://<your-codespace-url>-8081.app.github.dev/beta1`

Then:
1. Enter the target URL in the startup prompt.
2. Click **Load + Start AI**.
3. Press **Ctrl+I** any time to pause/resume manual mode.

## Tip
If the site does not load, make sure the port visibility is set to at least **Private** for your account, and that the app process is still running.
