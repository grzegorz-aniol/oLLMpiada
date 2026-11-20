import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_docs.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_docs", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_knowledge_base_is_consistent():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_image_targets_must_exist_but_are_not_documents():
    checker = load_checker()
    page = checker.DOCS / "page.md"

    linked, errors = checker.check_links(page, "![Hero](images/hero.webp)", {})
    assert (linked, errors) == (set(), [])

    _, errors = checker.check_links(page, "![Missing](images/missing.webp)", {})
    assert errors == ["docs/page.md: broken link -> images/missing.webp"]
