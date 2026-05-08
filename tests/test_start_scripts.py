from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StartScriptTests(unittest.TestCase):
    def test_agent_start_runs_from_project_root_with_unbuffered_logs(self):
        script = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")

        agent_start = script.index('"$ROOT/agent.py" start')
        self.assertLess(script.index('cd "$ROOT"'), agent_start)
        self.assertIn("PYTHONUNBUFFERED=1", script[:agent_start])

    def test_dashboard_start_uses_python_server_not_next(self):
        start = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
        stop = (PROJECT_ROOT / "stop.sh").read_text(encoding="utf-8")
        status = (PROJECT_ROOT / "status.sh").read_text(encoding="utf-8")

        self.assertIn('"$ROOT/dashboard_server.py"', start)
        self.assertIn("dashboard_server\\.py", stop)
        self.assertIn("dashboard_server\\.py", status)
        self.assertNotIn("npm run dev", start)
        self.assertNotIn("next-server", status)

    def test_github_deploy_script_keeps_systemd_always_on_path(self):
        script = (PROJECT_ROOT / "scripts" / "deploy_from_github.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("flock -n", script)
        self.assertIn('git fetch "$REMOTE" "$BRANCH"', script)
        self.assertIn('git reset --hard "$REMOTE/$BRANCH"', script)
        self.assertIn('venv/bin/python -m pip install -r requirements.txt', script)
        self.assertIn('systemctl restart "$WORKER_SERVICE"', script)
        self.assertIn('systemctl restart "$DASHBOARD_SERVICE"', script)
        self.assertIn('./status.sh', script)
        self.assertNotIn("docker compose", script)
        self.assertNotIn("serverless", script.lower())


if __name__ == "__main__":
    unittest.main()
