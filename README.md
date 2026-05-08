# Rapid X AI · LiveKit Voice Agent

Outbound voice agent built on **Gemini Live** (native audio), **LiveKit** (rooms + SIP) and a SIP/PSTN provider such as **Vobiz**, Twilio or Plivo. Comes with a lightweight Python + Tailwind dashboard for dispatching single or bulk calls and watching live sessions.

## Architecture

```
 ┌────────────┐   HTTP    ┌──────────────────┐   LiveKit RPC   ┌──────────────┐
 │ Dashboard  │──────────▶│  Python HTTP API │────────────────▶│  LiveKit     │
 │ (Tailwind) │           │  /api/dispatch   │                 │  Cloud       │
 └────────────┘           │  /api/queue      │                 │  + SIP trunk │
                          │  /api/calls      │◀────────────────│              │
                          └──────────────────┘                 └──────┬───────┘
                                                                       │ WebRTC
                                                                       ▼
                                                              ┌──────────────────┐
                                                              │  agent.py        │
                                                              │  Gemini Live     │
                                                              │  (STT+LLM+TTS)   │
                                                              └──────────────────┘
                                                                       │ SIP
                                                                       ▼
                                                                  SIP / PSTN
```

A single Gemini Live `RealtimeModel` replaces the old Deepgram + OpenAI/Groq + Cartesia/Sarvam pipeline — one streaming round-trip, lower latency, fewer keys to manage.

The default realtime model is `gemini-3.1-flash-live-preview`. Set `GEMINI_LIVE_MODEL` if you need to pin or roll back to another Gemini Live model.

Outbound calls need the bot to speak first. In LiveKit `1.5.7`, current Gemini `3.1` Live models do not support `generate_reply()`, so the worker automatically uses `GEMINI_PROACTIVE_REPLY_MODEL` for proactive greetings. Keep that set to a native-audio Gemini Live model such as `gemini-2.5-flash-native-audio-preview-12-2025`.

## Setup

### 1. Configure environment

```bash
cp .env.example .env
# Then edit .env with your real LiveKit, Gemini and Vobiz SIP credentials.
```

### 2. Python agent

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python agent.py start
```

### 3. Dashboard

```bash
python dashboard_server.py
# Opens on http://${HOST}:${PORT} (defaults to 0.0.0.0:3000)
```

Set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` in `.env` to protect the dashboard with browser Basic Auth.

### 4. Docker (agent + dashboard)

```bash
docker compose up --build
```

## GitHub push deploys

For the VPS deployment at `https://call-agent.protechplanner.com/`, GitHub can call the dashboard backend after every push to `main`:

```text
https://call-agent.protechplanner.com/api/deploy/github
```

Configure the GitHub repository webhook with:

- Content type: `application/json`
- Secret: the same long random value stored as `DEPLOY_WEBHOOK_SECRET` in `.env`
- Event: push

The webhook endpoint verifies GitHub's `X-Hub-Signature-256`, ignores non-`main` refs, and launches `scripts/deploy_from_github.sh` in the background. The script runs in `/opt/livekit-ai-voice` by default, pulls `origin/main`, installs Python dependencies, restarts the systemd worker/dashboard services, and runs `./status.sh` so each deploy checks the expected `India West` worker region.

Set these server env values in `.env` if your service names differ:

```bash
DEPLOY_WEBHOOK_SECRET=replace-with-a-long-random-secret
DEPLOY_RUNNER=auto
DEPLOY_REPO_DIR=/opt/livekit-ai-voice
DEPLOY_BRANCH=main
DEPLOY_WORKER_SERVICE=livekit-agent.service
DEPLOY_DASHBOARD_SERVICE=livekit-dashboard.service
DEPLOY_LOG=/opt/livekit-ai-voice/logs/deploy.log
EXPECTED_LIVEKIT_REGION="India West"
```

## SIP trunk management

```bash
python list_trunks.py            # list trunks on this LiveKit project
python create_trunk.py           # create one from .env (writes Vobiz creds)
python setup_trunk.py            # update an existing trunk in-place
```

## CLI dialing (no dashboard needed)

```bash
python make_call.py --to +919876543210 --voice Puck --prompt "Survey about coffee order"
python make_call.py --to +919876543210 --lead-context "Premium customer; last order delivered"
```

`--lead-context` is written into room metadata before the call starts, so the agent can answer from prefetched context instead of making slow mid-call CRM lookups.

## Low-latency guardrails

- Native Gemini Live audio-to-audio stays on the media path; no STT → LLM → TTS chain.
- LiveKit turn handling is explicit: 400 ms Gemini activity silence, 350 ms endpointing floor, and barge-in enabled.
- Room input audio frames are set to 20 ms.
- System prompt warnings appear in the dashboard around 300 estimated tokens.
- Structured latency metrics are written to `/tmp/voice-agent-metrics.jsonl` by default. Override with `VOICE_METRICS_LOG`.
- Run `./status.sh` after deploy to check the worker log for the expected `India West` region and recent metrics.

## Dashboard endpoints

| Route               | Method | Purpose                                      |
| ------------------- | ------ | -------------------------------------------- |
| `/api/dispatch`     | POST   | Dispatch one outbound call                   |
| `/api/queue`        | POST   | Bulk dispatch a list of numbers              |
| `/api/calls`        | GET    | List active call rooms                       |
| `/api/calls`        | DELETE | Hang up a specific room (`{ roomName }`)     |
| `/api/transcripts`  | GET    | Poll transcript JSONL records                |

## Files

- `agent.py` — LiveKit worker; runs the Gemini Live realtime model.
- `dashboard_server.py` — Python HTTP server for the Tailwind dashboard and API routes.
- `web/index.html` — static dashboard UI.
- `config.py` — central config; reads `.env`, supports legacy `VOBIZ_*` names.
- `make_call.py` — CLI to dispatch a single call via `AgentDispatch`.
- `create_trunk.py` / `setup_trunk.py` / `list_trunks.py` — SIP trunk admin.

## Troubleshooting

- **`GEMINI_API_KEY missing`** — set `GEMINI_API_KEY` in `.env`. If both `GEMINI_API_KEY` and `GOOGLE_API_KEY` are exported in your shell, the app now removes the conflicting Google key inside the worker so Gemini Live uses the intended key.
- **Worker logs an old Gemini model** — restart from this project folder or use `./start.sh`; `config.py` loads the project `.env` with override so stale terminal exports cannot pin an old model.
- **`generate_reply is not compatible with 'gemini-3.1-flash-live-preview'`** — this is a LiveKit adapter limitation for proactive bot speech. Set `GEMINI_PROACTIVE_REPLY_MODEL=gemini-2.5-flash-native-audio-preview-12-2025`; the code uses it automatically for outbound greetings while staying on native Gemini Live audio.
- **SIP 408 while dialing** — the agent creates the SIP participant without blocking on answer, keeps Gemini warm, and waits locally for the SIP participant to join before greeting.
- **`SIP_TRUNK_ID not configured`** — run `python list_trunks.py`; if empty, run `python create_trunk.py`.
- **Vobiz auth retries** — re-run `python setup_trunk.py` after rotating credentials.
- **Dashboard 500s** — make sure `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` are exported in the env that runs `python dashboard_server.py`.
- **Agent connects but no audio** — confirm the SIP trunk's `address` matches the Vobiz SIP domain exactly.
