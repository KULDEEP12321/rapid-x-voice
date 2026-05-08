import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DashboardServerTests(unittest.TestCase):
    def test_basic_auth_accepts_only_configured_credentials(self):
        env = {
            "DASHBOARD_USERNAME": "admin@example.com",
            "DASHBOARD_PASSWORD": "secret-password",
        }
        good = "Basic " + base64.b64encode(
            b"admin@example.com:secret-password"
        ).decode("ascii")
        wrong = "Basic " + base64.b64encode(
            b"admin@example.com:wrong-password"
        ).decode("ascii")

        self.assertTrue(dashboard_server.is_dashboard_authorized(good, env))
        self.assertFalse(dashboard_server.is_dashboard_authorized(wrong, env))
        self.assertFalse(dashboard_server.is_dashboard_authorized("", env))

    def test_basic_auth_is_disabled_without_complete_credentials(self):
        self.assertTrue(dashboard_server.is_dashboard_authorized("", {}))
        self.assertTrue(
            dashboard_server.is_dashboard_authorized(
                "",
                {"DASHBOARD_USERNAME": "admin@example.com"},
            )
        )

    def test_login_session_cookie_authorizes_dashboard(self):
        env = {
            "DASHBOARD_USERNAME": "admin@example.com",
            "DASHBOARD_PASSWORD": "secret-password",
            "DASHBOARD_SESSION_SECRET": "session-secret",
        }

        with patch.dict(os.environ, env, clear=False):
            cookie = dashboard_server.create_dashboard_session_cookie("admin@example.com")

        self.assertIn("dashboard_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertTrue(
            dashboard_server.is_dashboard_authorized(
                None,
                env,
                cookie_header=cookie,
            )
        )

    def test_login_session_cookie_rejects_tampered_value(self):
        env = {
            "DASHBOARD_USERNAME": "admin@example.com",
            "DASHBOARD_PASSWORD": "secret-password",
            "DASHBOARD_SESSION_SECRET": "session-secret",
        }

        self.assertFalse(
            dashboard_server.is_dashboard_authorized(
                None,
                env,
                cookie_header="dashboard_session=admin@example.com.bad-signature",
            )
        )

    def test_call_metadata_matches_agent_contract(self):
        metadata = dashboard_server.create_call_metadata(
            phone_number="+91 98765-43210",
            prompt="Admissions follow-up",
            voice="Aoede",
            temperature=0.4,
            system_prompt="You are Priya. Reply in 1-2 sentences.",
            lead_context="Parent asked about Grade 5 fees.",
        )

        parsed = json.loads(metadata)

        self.assertEqual(parsed["phone_number"], "+919876543210")
        self.assertEqual(parsed["user_prompt"], "Admissions follow-up")
        self.assertEqual(parsed["lead_context"], "Parent asked about Grade 5 fees.")
        self.assertEqual(parsed["system_prompt"], "You are Priya. Reply in 1-2 sentences.")
        self.assertEqual(parsed["voice_id"], "Aoede")
        self.assertEqual(parsed["temperature"], 0.4)

    def test_call_metadata_prefills_lead_context_from_prompt(self):
        metadata = dashboard_server.create_call_metadata(
            phone_number="+919876543210",
            prompt="Called yesterday about clinic appointment.",
            voice=None,
            temperature=None,
            system_prompt="",
            lead_context="",
        )

        parsed = json.loads(metadata)

        self.assertEqual(parsed["lead_context"], "Called yesterday about clinic appointment.")
        self.assertNotIn("voice_id", parsed)
        self.assertNotIn("temperature", parsed)
        self.assertNotIn("system_prompt", parsed)

    def test_bulk_numbers_are_sanitized_and_invalid_numbers_are_reported(self):
        rows = dashboard_server.parse_bulk_numbers(
            "+91 98765-43210\n9876543210, +1 (212) 555-1234"
        )

        self.assertEqual(
            rows,
            ["+919876543210", "9876543210", "+12125551234"],
        )
        self.assertTrue(dashboard_server.is_valid_e164("+919876543210"))
        self.assertFalse(dashboard_server.is_valid_e164("9876543210"))

    def test_transcripts_are_filtered_by_room_and_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"ts": 1.0, "room": "call-a", "role": "system", "text": "old", "is_final": True}),
                        json.dumps({"ts": 2.0, "room": "call-b", "role": "user", "text": "skip", "is_final": True}),
                        json.dumps({"ts": 3.0, "room": "call-a", "role": "agent", "text": "new", "is_final": True}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = dashboard_server.read_transcripts(path, room_filter="call-a", since=1.5, tail=200)

        self.assertEqual(payload["lastTs"], 3.0)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["text"], "new")

    def test_static_ui_uses_tailwind_and_python_api_routes(self):
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("cdn.tailwindcss.com", html)
        self.assertIn("/api/dispatch", html)
        self.assertIn("/api/queue", html)
        self.assertIn("/api/calls", html)
        self.assertIn("/api/transcripts", html)
        self.assertNotIn("_next", html)
        self.assertNotIn("DASHBOARD_PASSWORD", html)

    def test_login_ui_has_username_and_password_fields_without_secret(self):
        html = (PROJECT_ROOT / "web" / "login.html").read_text(encoding="utf-8")

        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertIn("/api/login", html)
        self.assertNotIn("Admin@123456", html)

    def test_next_dashboard_is_removed(self):
        self.assertFalse((PROJECT_ROOT / "dashboard").exists())

    def test_github_webhook_signature_accepts_valid_sha256(self):
        body = b'{"ref":"refs/heads/main"}'
        secret = "deploy-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        self.assertTrue(
            dashboard_server.verify_github_webhook_signature(body, signature, secret)
        )

    def test_github_webhook_signature_rejects_missing_or_bad_signature(self):
        body = b'{"ref":"refs/heads/main"}'

        self.assertFalse(
            dashboard_server.verify_github_webhook_signature(body, None, "secret")
        )
        self.assertFalse(
            dashboard_server.verify_github_webhook_signature(
                body,
                "sha256=bad",
                "secret",
            )
        )
        self.assertFalse(
            dashboard_server.verify_github_webhook_signature(body, "sha256=bad", "")
        )

    def test_github_webhook_ignores_non_main_push_without_deploy(self):
        payload = {"ref": "refs/heads/feature"}
        body = json.dumps(payload).encode("utf-8")
        secret = "deploy-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        calls = []
        status, response = dashboard_server.handle_github_deploy_webhook(
            body,
            {
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
            },
            env={"DEPLOY_WEBHOOK_SECRET": secret, "DEPLOY_BRANCH": "main"},
            start_deploy=lambda payload: calls.append(payload),
        )

        self.assertEqual(status, 202)
        self.assertFalse(response["accepted"])
        self.assertEqual(response["reason"], "ignored ref")
        self.assertEqual(calls, [])

    def test_github_webhook_accepts_main_push_and_triggers_deploy(self):
        payload = {"ref": "refs/heads/main", "after": "abc123"}
        body = json.dumps(payload).encode("utf-8")
        secret = "deploy-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        calls = []
        status, response = dashboard_server.handle_github_deploy_webhook(
            body,
            {
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
            },
            env={"DEPLOY_WEBHOOK_SECRET": secret, "DEPLOY_BRANCH": "main"},
            start_deploy=lambda payload: calls.append(payload),
        )

        self.assertEqual(status, 202)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["commit"], "abc123")
        self.assertEqual(calls, [payload])

    def test_start_github_deploy_runs_script_in_background_with_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "deploy_from_github.sh"
            script.parent.mkdir()
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            log = root / "logs" / "deploy.log"
            payload = {"after": "abc123"}

            with patch("dashboard_server.subprocess.Popen") as popen:
                dashboard_server.start_github_deploy(
                    payload,
                    env={
                        "DEPLOY_REPO_DIR": str(root),
                        "DEPLOY_SCRIPT": str(script),
                        "DEPLOY_LOG": str(log),
                    },
                )

                self.assertTrue(log.exists())
                popen.assert_called_once()
                args, kwargs = popen.call_args
                self.assertEqual(args[0], [str(script)])
                self.assertEqual(kwargs["cwd"], str(root))
                self.assertTrue(kwargs["start_new_session"])
                self.assertEqual(kwargs["env"]["GITHUB_DEPLOY_COMMIT"], "abc123")


if __name__ == "__main__":
    unittest.main()
