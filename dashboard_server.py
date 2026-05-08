"""Small Python dashboard for dispatching LiveKit outbound voice calls.

The media path stays in ``agent.py``. This server only serves a static UI and
creates LiveKit rooms plus agent dispatches.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import random
import re
import subprocess
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

from livekit import api

import config


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "web"
TRANSCRIPT_LOG = Path(os.getenv("TRANSCRIPT_LOG", "/tmp/transcripts.jsonl"))
AGENT_NAME = os.getenv("AGENT_NAME", "outbound-caller")
PROMPT_TOKEN_TARGET = int(os.getenv("REALTIME_PROMPT_TOKEN_TARGET", "300"))
DASHBOARD_AUTH_REALM = "Rapid X Dashboard"
DASHBOARD_SESSION_COOKIE = "dashboard_session"
DASHBOARD_SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
GITHUB_DEPLOY_PATH = "/api/deploy/github"

logger = logging.getLogger("dashboard-server")
logging.basicConfig(level=logging.INFO)


def sanitize_phone_number(phone_number: str) -> str:
    return re.sub(r"[^\d+]", "", str(phone_number or "").strip())


def is_valid_e164(phone_number: str) -> bool:
    phone = sanitize_phone_number(phone_number)
    return bool(re.fullmatch(r"\+\d{8,15}", phone))


def parse_bulk_numbers(value: str) -> list[str]:
    return [
        sanitize_phone_number(part)
        for part in re.split(r"[\n,]+", value or "")
        if sanitize_phone_number(part)
    ]


def _optional_text(value) -> str:
    return str(value or "").strip()


def _dashboard_credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    source = env if env is not None else os.environ
    username = _optional_text(source.get("DASHBOARD_USERNAME"))
    password = str(source.get("DASHBOARD_PASSWORD") or "")
    if not username or not password:
        return None
    return username, password


def _session_secret(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return (
        str(source.get("DASHBOARD_SESSION_SECRET") or "")
        or str(source.get("DASHBOARD_PASSWORD") or "")
        or "dashboard-dev-session-secret"
    )


def _sign_session_payload(payload: str, env: dict[str, str] | None = None) -> str:
    return hmac.new(
        _session_secret(env).encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_dashboard_session_cookie(username: str, *, secure: bool = False) -> str:
    expires = int(time.time()) + DASHBOARD_SESSION_TTL_SECONDS
    payload = f"{username}|{expires}"
    token_body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    token = f"{token_body}.{_sign_session_payload(token_body)}"
    parts = [
        f"{DASHBOARD_SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={DASHBOARD_SESSION_TTL_SECONDS}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_dashboard_session_cookie() -> str:
    return (
        f"{DASHBOARD_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; "
        "Max-Age=0"
    )


def _session_cookie_authorized(
    cookie_header: str | None,
    credentials: tuple[str, str],
    env: dict[str, str] | None = None,
) -> bool:
    if not cookie_header:
        return False
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return False
    morsel = cookie.get(DASHBOARD_SESSION_COOKIE)
    if not morsel or "." not in morsel.value:
        return False

    token_body, signature = morsel.value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign_session_payload(token_body, env)):
        return False

    try:
        payload = base64.urlsafe_b64decode(token_body.encode("ascii")).decode("utf-8")
        username, expires_text = payload.rsplit("|", 1)
        expires = int(expires_text)
    except Exception:
        return False

    expected_username, _ = credentials
    return username == expected_username and expires >= int(time.time())


def is_dashboard_authorized(
    authorization_header: str | None,
    env: dict[str, str] | None = None,
    *,
    cookie_header: str | None = None,
) -> bool:
    credentials = _dashboard_credentials(env)
    if credentials is None:
        return True
    if _session_cookie_authorized(cookie_header, credentials, env):
        return True
    if not authorization_header or not authorization_header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(
            authorization_header.removeprefix("Basic ").strip(),
            validate=True,
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    expected_username, expected_password = credentials
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password,
        expected_password,
    )


def create_call_metadata(
    *,
    phone_number: str,
    prompt: str | None = None,
    voice: str | None = None,
    temperature=None,
    system_prompt: str | None = None,
    lead_context: str | None = None,
) -> str:
    user_prompt = _optional_text(prompt)
    lead = _optional_text(lead_context) or user_prompt
    payload: dict[str, object] = {
        "phone_number": sanitize_phone_number(phone_number),
        "user_prompt": user_prompt,
    }
    if lead:
        payload["lead_context"] = lead
    if _optional_text(system_prompt):
        payload["system_prompt"] = _optional_text(system_prompt)
    if _optional_text(voice):
        payload["voice_id"] = _optional_text(voice)
    if temperature is not None and temperature != "":
        payload["temperature"] = float(temperature)
    return json.dumps(payload, separators=(",", ":"))


def estimate_prompt_tokens(text: str) -> int:
    words = [word for word in str(text or "").strip().split() if word]
    return int((len(words) * 1.3) + 0.999)


def verify_github_webhook_signature(
    body: bytes,
    signature_header: str | None,
    secret: str | None,
) -> bool:
    secret = str(secret or "")
    if not secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    expected = prefix + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def _deploy_branch(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get("DEPLOY_BRANCH") or "main")


def _deploy_ref(env: Mapping[str, str] | None = None) -> str:
    return f"refs/heads/{_deploy_branch(env)}"


def start_github_deploy(
    payload: dict,
    env: Mapping[str, str] | None = None,
) -> None:
    source = env if env is not None else os.environ
    repo_dir = Path(source.get("DEPLOY_REPO_DIR") or ROOT)
    script = Path(source.get("DEPLOY_SCRIPT") or (repo_dir / "scripts" / "deploy_from_github.sh"))
    log_path = Path(source.get("DEPLOY_LOG") or (repo_dir / "logs" / "deploy.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    process_env = os.environ.copy()
    process_env.update({str(k): str(v) for k, v in source.items()})
    process_env["GITHUB_DEPLOY_COMMIT"] = str(payload.get("after") or "")
    process_env["GITHUB_DEPLOY_REF"] = str(payload.get("ref") or "")

    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [str(script)],
            cwd=str(repo_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=process_env,
            start_new_session=True,
            close_fds=True,
        )


def handle_github_deploy_webhook(
    body: bytes,
    headers: Mapping[str, str | None],
    *,
    env: Mapping[str, str] | None = None,
    start_deploy=start_github_deploy,
) -> tuple[HTTPStatus, dict]:
    source = env if env is not None else os.environ
    secret = source.get("DEPLOY_WEBHOOK_SECRET")
    if not secret:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "accepted": False,
            "error": "deploy webhook disabled",
        }
    if not verify_github_webhook_signature(
        body,
        headers.get("X-Hub-Signature-256"),
        secret,
    ):
        return HTTPStatus.UNAUTHORIZED, {
            "accepted": False,
            "error": "invalid signature",
        }

    event = str(headers.get("X-GitHub-Event") or "")
    if event == "ping":
        return HTTPStatus.OK, {"accepted": False, "reason": "pong"}
    if event != "push":
        return HTTPStatus.ACCEPTED, {"accepted": False, "reason": "ignored event"}

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return HTTPStatus.BAD_REQUEST, {"accepted": False, "error": "invalid json"}
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, {"accepted": False, "error": "invalid payload"}

    if payload.get("ref") != _deploy_ref(source):
        return HTTPStatus.ACCEPTED, {"accepted": False, "reason": "ignored ref"}

    start_deploy(payload)
    return HTTPStatus.ACCEPTED, {
        "accepted": True,
        "commit": str(payload.get("after") or ""),
    }


def _require_livekit_config() -> None:
    missing = [
        name
        for name, value in (
            ("LIVEKIT_URL", config.LIVEKIT_URL),
            ("LIVEKIT_API_KEY", config.LIVEKIT_API_KEY),
            ("LIVEKIT_API_SECRET", config.LIVEKIT_API_SECRET),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing LiveKit credentials: {', '.join(missing)}")


def _new_room_name(phone_number: str) -> str:
    safe_phone = sanitize_phone_number(phone_number).replace("+", "")
    return f"call-{safe_phone}-{random.randrange(10000):04d}"


async def _with_livekit(fn):
    _require_livekit_config()
    client = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )
    try:
        return await fn(client)
    finally:
        await client.aclose()


async def _dispatch_with_client(client: api.LiveKitAPI, payload: dict) -> dict:
    phone_number = sanitize_phone_number(payload.get("phoneNumber") or payload.get("phone_number"))
    if not phone_number:
        raise ValueError("phoneNumber is required")
    if not is_valid_e164(phone_number):
        raise ValueError("phoneNumber must be E.164, for example +919876543210")

    room_name = _new_room_name(phone_number)
    metadata = create_call_metadata(
        phone_number=phone_number,
        prompt=payload.get("prompt"),
        lead_context=payload.get("leadContext") or payload.get("lead_context"),
        system_prompt=payload.get("systemPrompt") or payload.get("system_prompt"),
        voice=payload.get("voice"),
        temperature=payload.get("temperature"),
    )

    await client.room.create_room(
        api.CreateRoomRequest(name=room_name, metadata=metadata, empty_timeout=60 * 5)
    )
    dispatch = await client.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            room=room_name,
            agent_name=AGENT_NAME,
            metadata=metadata,
        )
    )

    return {"success": True, "roomName": room_name, "dispatchId": dispatch.id}


async def dispatch_call(payload: dict) -> dict:
    return await _with_livekit(lambda client: _dispatch_with_client(client, payload))


async def dispatch_queue(payload: dict) -> dict:
    numbers = payload.get("numbers")
    if not isinstance(numbers, list) or not numbers:
        raise ValueError("numbers[] required")

    async def run(client):
        results = []
        for raw in numbers:
            phone_number = sanitize_phone_number(raw)
            if not is_valid_e164(phone_number):
                results.append(
                    {
                        "phoneNumber": phone_number,
                        "status": "failed",
                        "error": "must start with + and include country code",
                    }
                )
                continue
            try:
                result = await _dispatch_with_client(
                    client,
                    {
                        **payload,
                        "phoneNumber": phone_number,
                    },
                )
                results.append(
                    {
                        "phoneNumber": phone_number,
                        "status": "dispatched",
                        "id": result["dispatchId"],
                    }
                )
            except Exception as exc:
                logger.exception("Failed to dispatch %s", phone_number)
                results.append(
                    {
                        "phoneNumber": phone_number,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            await asyncio.sleep(0.2)

        return {
            "success": True,
            "message": f"Processed {len(numbers)} numbers",
            "results": results,
        }

    return await _with_livekit(run)


async def list_active_calls() -> dict:
    async def run(client):
        response = await client.room.list_rooms(api.ListRoomsRequest())
        calls = []
        for room in response.rooms:
            if not room.name.startswith("call-"):
                continue
            metadata = {}
            if room.metadata:
                try:
                    metadata = json.loads(room.metadata)
                except json.JSONDecodeError:
                    metadata = {}
            creation_time = int(room.creation_time or (room.creation_time_ms // 1000))
            calls.append(
                {
                    "roomName": room.name,
                    "sid": room.sid,
                    "numParticipants": room.num_participants,
                    "creationTime": creation_time,
                    "phone": metadata.get("phone_number"),
                    "voice": metadata.get("voice_id"),
                    "prompt": metadata.get("user_prompt", ""),
                }
            )
        calls.sort(key=lambda item: item["creationTime"], reverse=True)
        return {"calls": calls}

    return await _with_livekit(run)


async def delete_call_room(room_name: str) -> dict:
    if not room_name:
        raise ValueError("roomName required")

    async def run(client):
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        return {"success": True}

    return await _with_livekit(run)


def read_transcripts(
    path: Path = TRANSCRIPT_LOG,
    *,
    room_filter: str | None = None,
    since: float = 0,
    tail: int = 200,
) -> dict:
    tail = max(0, min(500, int(tail or 200)))
    if not path.exists():
        return {"records": [], "lastTs": since}

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if room_filter and record.get("room") != room_filter:
            continue
        if since > 0 and float(record.get("ts", 0)) <= since:
            continue
        records.append(record)

    out = records[-tail:] if since == 0 else records
    last_ts = out[-1]["ts"] if out else since
    return {"records": out, "lastTs": last_ts}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "RapidXDashboard/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._send_file(STATIC_ROOT / "login.html", "text/html; charset=utf-8")
            return
        if parsed.path in ("/", "/index.html") and not self._is_authorized():
            self._send_file(STATIC_ROOT / "login.html", "text/html; charset=utf-8")
            return
        if parsed.path != "/healthz" and not self._is_authorized():
            self._send_auth_required()
            return
        if parsed.path in ("/", "/index.html"):
            self._send_file(STATIC_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self._send_json({"ok": True, "ts": time.time()})
            return
        if parsed.path == "/api/calls":
            self._handle_async(list_active_calls())
            return
        if parsed.path == "/api/transcripts":
            params = parse_qs(parsed.query)
            payload = read_transcripts(
                TRANSCRIPT_LOG,
                room_filter=(params.get("room") or [None])[0],
                since=float((params.get("since") or ["0"])[0] or 0),
                tail=int((params.get("tail") or ["200"])[0] or 200),
            )
            self._send_json(payload)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == GITHUB_DEPLOY_PATH:
            self._handle_github_deploy()
            return
        if parsed.path == "/api/login":
            self._handle_login()
            return
        if parsed.path == "/api/logout":
            self._send_json(
                {"success": True},
                headers={"Set-Cookie": clear_dashboard_session_cookie()},
            )
            return
        if not self._is_authorized():
            self._send_auth_required()
            return
        try:
            payload = self._read_json()
            if parsed.path == "/api/dispatch":
                self._handle_async(dispatch_call(payload))
                return
            if parsed.path == "/api/queue":
                self._handle_async(dispatch_queue(payload))
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            logger.exception("POST %s failed", parsed.path)
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self._is_authorized():
            self._send_auth_required()
            return
        try:
            if parsed.path != "/api/calls":
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = self._read_json()
            self._handle_async(delete_call_room(_optional_text(payload.get("roomName"))))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            logger.exception("DELETE %s failed", parsed.path)
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_async(self, awaitable):
        try:
            self._send_json(asyncio.run(awaitable))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            logger.exception("request failed")
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_login(self):
        credentials = _dashboard_credentials()
        if credentials is None:
            self._send_json({"success": True})
            return
        payload = self._read_json()
        username = _optional_text(payload.get("username"))
        password = str(payload.get("password") or "")
        expected_username, expected_password = credentials
        if not (
            hmac.compare_digest(username, expected_username)
            and hmac.compare_digest(password, expected_password)
        ):
            self._send_json(
                {"error": "Invalid username or password"},
                HTTPStatus.UNAUTHORIZED,
            )
            return
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        self._send_json(
            {"success": True},
            headers={"Set-Cookie": create_dashboard_session_cookie(username, secure=secure)},
        )

    def _handle_github_deploy(self):
        status, payload = handle_github_deploy_webhook(
            self._read_body(),
            self.headers,
        )
        self._send_json(payload, status)

    def _is_authorized(self) -> bool:
        return is_dashboard_authorized(
            self.headers.get("Authorization"),
            cookie_header=self.headers.get("Cookie"),
        )

    def _send_auth_required(self):
        body = b"Authentication required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header(
            "WWW-Authenticate",
            f'Basic realm="{DASHBOARD_AUTH_REALM}", charset="UTF-8"',
        )
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        raw = self._read_body().decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self._send_json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(path.read_bytes(), content_type)

    def _send_json(
        self,
        payload: dict,
        status: int | HTTPStatus = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status, headers=headers)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int | HTTPStatus = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ):
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str | None = None, port: int | None = None):
    host = host or config.DASHBOARD_HOST
    port = int(port or config.DASHBOARD_PORT)
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    logger.info("Dashboard listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
