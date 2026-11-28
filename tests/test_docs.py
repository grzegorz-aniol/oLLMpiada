import importlib.util
import subprocess
import sys
from pathlib import Path

from ollmpiada.cli import MIN_BOARDS
from ollmpiada.competition import DEFAULT_MAX_TURNS
from ollmpiada.game_master import ATTEMPTS_PER_TURN
from ollmpiada.games.base import OPENING_MOVES
from ollmpiada.games.bridge import FIT
from ollmpiada.games.chess_game import ADJUDICATION_WIN_CP

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


def test_anchors_into_the_repository_readme_must_exist(tmp_path, monkeypatch, capsys):
    checker = load_checker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "README.md").write_text("# Project\n\n## Present\n", encoding="utf-8")
    (docs / "README.md").write_text(
        "[Present](../README.md#present) [Absent](../README.md#absent)\n", encoding="utf-8"
    )
    monkeypatch.setattr(checker, "DOCS", docs)
    monkeypatch.setattr(checker, "ROOT_INDEX", docs / "README.md")
    monkeypatch.setattr(checker, "REPO_README", tmp_path / "README.md")

    assert checker.main() == 1
    errors = [line for line in capsys.readouterr().out.splitlines() if line.startswith("ERROR")]
    assert errors == ["ERROR: docs/README.md: missing anchor -> ../README.md#absent"]


def test_readme_numbers_match_the_code_constants():
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    ordinal = {3: "third"}[ATTEMPTS_PER_TURN]
    for phrase in (
        f"In each side's first {OPENING_MOVES} moves it can only move",
        f"`--max-turns` moves (default {DEFAULT_MAX_TURNS})",
        f"at least {ADJUDICATION_WIN_CP / 100:g} pawns ahead",
        f"or a {ordinal} rejected answer in one turn, which loses",
        f"longest suit of at least {FIT} cards",
        f"fewer than {MIN_BOARDS} boards",
        f"or at a {ordinal} rejected answer in one turn",
    ):
        assert phrase in readme
