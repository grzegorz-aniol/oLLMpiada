"""Golden test: the exact model-facing requests of every game, pinned in `tests/golden/`, so a
change to what a model reads is a deliberate decision (docs/testing.md#golden-prompts)."""

import asyncio
import dataclasses
import difflib
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from ollmpiada.competition import prompt_version, sample_requests
from ollmpiada.config import Model, load_models
from ollmpiada.games.base import Game
from ollmpiada.players.base import TurnRequest
from ollmpiada.players.decision import DecisionPlayer
from ollmpiada.players.llm import answer_type, llm_message
from ollmpiada.registry import MASTERS

GOLDEN = Path(__file__).parent / "golden"
UPDATE = "UPDATE_GOLDEN"
REGENERATE = f"{UPDATE}=1 uv run pytest tests/test_golden.py"

# The models read the prompts with the real opening, so the golden files do too.
pytestmark = pytest.mark.opening


@pytest.fixture
def decision_model(models_file: Path) -> Model:
    return load_models(models_file, {"TEST_HOSTED_KEY": "secret"})["basal-mini"]


@pytest.mark.parametrize("name", list(MASTERS))
def test_model_facing_requests_match_the_golden_files(name: str, decision_model: Model):
    expected = golden_files(MASTERS[name].game, decision_model)
    folder = GOLDEN / name
    if os.environ.get(UPDATE):
        shutil.rmtree(folder, ignore_errors=True)
        for path, text in expected.items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(text, encoding="utf-8")
    stored = {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }
    changed = sorted(p for p in expected.keys() | stored.keys() if stored.get(p) != expected.get(p))
    if changed:
        # A missing file diffs as all added lines, a file no longer rendered as all removed ones.
        diffs = (
            difflib.unified_diff(
                stored.get(path, "").splitlines(keepends=True),
                expected.get(path, "").splitlines(keepends=True),
                f"golden/{name}/{path}",
                f"rendered {path}",
            )
            for path in changed
        )
        pytest.fail(
            f"What the {name} models read differs from the golden files in tests/golden/{name}: "
            f"{', '.join(changed)}. If the change is deliberate, regenerate them with "
            f"`{REGENERATE}` and name the new prompt version "
            "(docs/game-master/prompts.md#prompts).\n" + "".join(map("".join, diffs)),
            pytrace=False,
        )


def golden_files(game: Game, decision_model: Model) -> dict[str, str]:
    """Every pinned text of `game` by its path in the game's golden folder: the prompt version,
    then per sample turn the LLM message, the LLM answer schema and the decision model's state
    and questions, the latter also asked per piece (with `max_options` one below the legal
    moves)."""
    files = {"prompt_version.txt": prompt_version(game) + "\n"}
    for turn, request in sample_requests(game).items():
        whole = decision_payload(decision_model, request)
        by_piece = decision_payload(decision_model, request, len(request.legal_moves) - 1)
        files |= {
            f"{turn}/llm_message.txt": llm_message(request.prompt),
            f"{turn}/answer_schema.json": _json(answer_type(request.move_only).model_json_schema()),
            f"{turn}/decision_state.txt": whole["state"],
            f"{turn}/decision_questions.json": _json(whole["questions"]),
            f"{turn}/decision_questions_by_piece.json": _json(by_piece["questions"]),
        }
    return files


def decision_payload(model: Model, request: TurnRequest, max_options: int | None = None) -> dict:
    """The JSON body the decision player posts for `request`."""
    if max_options:
        model = dataclasses.replace(
            model, spec=model.spec.model_copy(update={"max_options": max_options})
        )
    sent = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return httpx.Response(200, json={})

    player = DecisionPlayer("golden", model, timeout_s=5, transport=httpx.MockTransport(handler))
    asyncio.run(player.play(request))
    return sent[0]


def _json(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
