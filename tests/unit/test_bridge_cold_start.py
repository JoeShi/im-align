import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "scripts" / "bridge.py"


class BridgeColdStartTests(unittest.TestCase):
    def test_help_does_not_load_feishu_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "lark_oapi"
            stub.mkdir()
            (stub / "__init__.py").write_text(
                'raise RuntimeError("lark_oapi must not load for --help")\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (tmp, str(ROOT), env.get("PYTHONPATH", "")) if part
            )

            result = subprocess.run(
                [sys.executable, str(BRIDGE), "--help"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: bridge.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
