"""Experiments: a folder with a list of competitions to play and the runs saved inside it."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ollmpiada.competition import check_games, default_max_turns
from ollmpiada.config import Model
from ollmpiada.players import PlayerError, resolve_player
from ollmpiada.registry import MASTERS

EXPERIMENT_FILE = "experiment.json"


class ExperimentError(Exception):
    """An experiment that is missing or whose `experiment.json` is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CompetitionEntry(_Strict):
    players: tuple[str, str]
    runs: Annotated[int, Field(ge=1)] = 1


class _ExperimentFile(_Strict):
    game: str
    games: Annotated[int, Field(ge=1)]
    max_turns: Annotated[int, Field(ge=1)] | None = None
    models: dict[str, str]
    competitions: Annotated[list[_CompetitionEntry], Field(min_length=1)]


@dataclass(frozen=True)
class Planned:
    """One competition the experiment wants: its players in side order, as `<id>[@effort]`
    names, and how many finished runs of it."""

    players: tuple[str, str]
    runs: int


@dataclass(frozen=True)
class Experiment:
    folder: Path
    """`<experiments root>/<id>`: the folder name is the experiment's short ID."""
    game: str
    games: int
    max_turns: int | None
    """Resolved like `ollmpiada run` does: the default for a game with a turn limit, else None."""
    competitions: tuple[Planned, ...]

    @property
    def runs_dir(self) -> Path:
        return self.folder / "runs"


@dataclass(frozen=True)
class RunFolder:
    path: Path
    status: str


@dataclass(frozen=True)
class Progress:
    """What `runs/` holds of one planned competition: its run folders with the same players
    and settings, oldest first."""

    planned: Planned
    runs: tuple[RunFolder, ...]

    @property
    def finished(self) -> int:
        return sum(run.status == "finished" for run in self.runs)

    @property
    def failed(self) -> int:
        return len(self.runs) - self.finished

    @property
    def done(self) -> bool:
        return self.finished >= self.planned.runs


def load_experiment(folder: Path, models: dict[str, Model]) -> Experiment:
    """Read and validate `<folder>/experiment.json`; models are checked like `ollmpiada run`
    checks them.

    Raises:
        ExperimentError: The file is missing, not JSON, or breaks a rule; the message names it.
    """
    path = folder / EXPERIMENT_FILE
    try:
        data = _ExperimentFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return _experiment(folder, data, models)
    # Bad JSON, a failed validation and a broken rule are all ValueErrors.
    except (OSError, ValueError) as e:
        raise ExperimentError(f"{path}: {e}") from e


def _experiment(folder: Path, data: _ExperimentFile, models: dict[str, Model]) -> Experiment:
    """Check the rules the file's shape cannot express and resolve the short names.

    Raises:
        ValueError: A rule is broken; the message says which.
    """
    master = MASTERS.get(data.game)
    if master is None:
        raise ValueError(f"unknown game {data.game!r}; known: {', '.join(MASTERS)}")
    if data.max_turns is not None and not master.game.turn_limit:
        raise ValueError(f"{data.game} has no turn limit; drop max_turns")
    check_games(master.game, data.games)
    for short, name in data.models.items():
        try:
            resolve_player(name, models)
        except PlayerError as e:
            raise ValueError(f"model {short!r}: {e}") from e
    planned: list[Planned] = []
    for entry in data.competitions:
        for short in entry.players:
            if short not in data.models:
                raise ValueError(f"unknown short name {short!r}; known: {', '.join(data.models)}")
        first, second = (data.models[short] for short in entry.players)
        if first == second:
            raise ValueError(f"pick two different players, not {first} twice")
        if any(p.players == (first, second) for p in planned):
            raise ValueError(f"competition {first} vs {second} is listed twice")
        planned.append(Planned((first, second), entry.runs))
    return Experiment(
        folder=folder,
        game=data.game,
        games=data.games,
        max_turns=data.max_turns or default_max_turns(master.game),
        competitions=tuple(planned),
    )


def progress(experiment: Experiment) -> list[Progress]:
    """The progress of each planned competition, in file order, read from `runs/`.

    A run folder belongs to a competition when its `competition.json` names the same players
    in the same order and the experiment's game, games and max turns; folders that match none,
    or have no readable `competition.json`, are ignored.
    """
    runs: dict[tuple[str, str], list[RunFolder]] = {p.players: [] for p in experiment.competitions}
    folders = sorted(experiment.runs_dir.iterdir()) if experiment.runs_dir.is_dir() else []
    for folder in folders:
        try:
            saved = json.loads((folder / "competition.json").read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        settings = (saved.get("game"), saved.get("games"), saved.get("max_turns"))
        players = tuple(saved.get("players", ()))
        wanted = (experiment.game, experiment.games, experiment.max_turns)
        if players in runs and settings == wanted:
            runs[players].append(RunFolder(folder, saved.get("status", "")))
    return [Progress(p, tuple(runs[p.players])) for p in experiment.competitions]
