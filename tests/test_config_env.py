import os
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_config_probe(env: dict[str, str], dotenv_body: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".env").write_text(dotenv_body, encoding="utf-8")
        base_env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_LIVE_MODEL"}
        }
        probe = textwrap.dedent(
            """
            import json
            import os
            import config

            print(json.dumps({
                "model": config.GEMINI_LIVE_MODEL,
                "key": config.GEMINI_API_KEY,
                "google_key_present": bool(os.getenv("GOOGLE_API_KEY")),
            }))
            """
        )
        return subprocess.run(
            [sys.executable, "-c", probe],
            env={
                **base_env,
                **env,
                "PYTHONPATH": str(PROJECT_ROOT),
                "VOICE_AGENT_DOTENV": str(Path(tmp) / ".env"),
            },
            text=True,
            capture_output=True,
            check=False,
        )


class DotenvPrecedenceTests(unittest.TestCase):
    def test_dotenv_model_overrides_stale_exported_shell_value(self):
        result = _run_config_probe(
            {"GEMINI_LIVE_MODEL": "gemini-2.5-flash-native-audio-preview-12-2025"},
            "GEMINI_LIVE_MODEL=gemini-2.5-flash-native-audio-preview-12-2025\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "model": "gemini-2.5-flash-native-audio-preview-12-2025",
                "key": None,
                "google_key_present": False,
            },
        )

    def test_gemini_key_wins_and_conflicting_google_key_is_removed(self):
        result = _run_config_probe(
            {
                "GEMINI_API_KEY": "stale-gemini-key",
                "GOOGLE_API_KEY": "wrong-google-key",
            },
            "GEMINI_API_KEY=correct-gemini-key\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "model": "gemini-2.5-flash-native-audio-preview-12-2025",
                "key": "correct-gemini-key",
                "google_key_present": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
