import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_docs_knowledge_base_is_consistent():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_docs.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
