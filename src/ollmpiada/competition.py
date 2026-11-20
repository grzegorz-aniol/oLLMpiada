"""Competition runner: a series of games between two players, with statistics and results."""

import asyncio
import dataclasses
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from ollmpiada.game_master import TRANSPORT_BACKOFF_S, Event, GameResult, play_game
from ollmpiada.games.base import Game, Side
from ollmpiada.players.base import Player, Usage
from ollmpiada.players.llm import ANSWER_FORMAT

Status = Literal["running", "finished", "aborted", "stopped"]


@dataclass
class PlayerStats:
    """Totals for one player (model id plus effort) over its counted games."""

    name: str
    games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    moves: int = 0
    calls: int = 0
    thinking_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls_without_usage: int = 0
    calls_without_reasoning: int = 0
    illegal_attempts: int = 0

    @property
    def tokens_known(self) -> bool:
        return self.calls > self.calls_without_usage

    @property
    def reasoning_known(self) -> bool:
        return self.calls - self.calls_without_usage > self.calls_without_reasoning

    def add(self, result: GameResult) -> None:
        """Count one finished game."""
        assert result.outcome is not None
        side = Side.FIRST if result.players[0] == self.name else Side.SECOND
        self.games += 1
        if result.outcome.winner is None:
            self.draws += 1
        elif result.outcome.winner is side:
            self.wins += 1
        else:
            self.losses += 1
        for turn in result.turns:
            if turn.side is not side:
                continue
            self.moves += turn.action == "move"
            for attempt in turn.attempts:
                self.add_call(attempt.elapsed_s, attempt.usage)
                self.illegal_attempts += attempt.rejection is not None

    def add_call(self, elapsed_s: float, usage: Usage | None) -> None:
        """Count one answered turn request, accepted or rejected."""
        self.calls += 1
        self.thinking_s += elapsed_s
        if usage is None:
            self.calls_without_usage += 1
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.reasoning_tokens is None:
            self.calls_without_reasoning += 1
        else:
            self.reasoning_tokens += usage.reasoning_tokens


@dataclass(frozen=True)
class GameSummary:
    """One row of the results table."""

    number: int
    players: tuple[str, str]
    """Player names in side order."""
    winner: str | None
    result: str
    """`1-0`, `0-1`, `1/2-1/2`, or `aborted`."""
    reason: str
    moves: int

    @classmethod
    def of(cls, result: GameResult) -> GameSummary:
        moves = sum(turn.action == "move" for turn in result.turns)
        if result.outcome is None:
            return cls(result.number, result.players, None, "aborted", result.error or "", moves)
        winner = result.outcome.winner
        score = {Side.FIRST: "1-0", Side.SECOND: "0-1", None: "1/2-1/2"}[winner]
        name = None if winner is None else result.players[0 if winner is Side.FIRST else 1]
        return cls(result.number, result.players, name, score, result.outcome.reason, moves)


@dataclass
class CompetitionState:
    """What the competition looks like now; every competition event carries it."""

    game: str
    side_names: tuple[str, str]
    players: tuple[str, str]
    games: int
    max_turns: int
    stats: dict[str, PlayerStats]
    results: list[GameSummary] = field(default_factory=list)
    status: Status = "running"
    error: str | None = None


@dataclass(frozen=True)
class CompetitionProgress:
    state: CompetitionState


@dataclass(frozen=True)
class CompetitionFinished:
    state: CompetitionState


CompetitionEvent = Event | CompetitionProgress | CompetitionFinished


def prompt_version(game: Game) -> str:
    """Hash of rendered prompts, so it covers both templates, every wording branch in code and
    the LLM answer format."""
    state = game.new_state()
    plain = game.context(state, turn=1, max_turns=1)
    retry = game.context(state, turn=1, max_turns=1, draw_offered=True, rejection="-")
    # repr keeps the system/user boundary, so moving text between the parts changes the hash.
    text = repr((game.prompt(plain), game.prompt(retry), ANSWER_FORMAT))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class Competition:
    def __init__(
        self,
        game: Game,
        players: tuple[Player, Player],
        *,
        games: int,
        max_turns: int,
        runs_dir: Path,
        publish: Callable[[CompetitionEvent], None] = lambda event: None,
        backoff_s: tuple[float, ...] = TRANSPORT_BACKOFF_S,
    ) -> None:
        self._game = game
        self._players = players
        self._publish = publish
        self._backoff_s = backoff_s
        self._current: asyncio.Task | None = None
        self._stop_requested = False
        names = (players[0].name, players[1].name)
        if names[0] == names[1]:
            raise ValueError(f"both players are named {names[0]!r}; statistics need two names")
        self.state = CompetitionState(
            game=game.name,
            side_names=game.side_names,
            players=names,
            games=games,
            max_turns=max_turns,
            stats={name: PlayerStats(name) for name in names},
        )
        self._prompt_version = prompt_version(game)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = runs_dir / f"{stamp}_{_safe(names[0])}_vs_{_safe(names[1])}"

    def stop(self) -> None:
        """Abort the game in progress and end the competition; finished games stay saved."""
        self._stop_requested = True
        if self._current:
            self._current.cancel()

    async def run(self) -> CompetitionState:
        self.run_dir.parent.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir()  # fails loudly instead of mixing two runs started in one second
        self._save()
        self._publish(CompetitionProgress(self.state))
        for number in range(1, self.state.games + 1):
            if self._stop_requested:
                break
            first, second = self._players
            pair = (first, second) if number % 2 == 1 else (second, first)
            self._current = asyncio.create_task(
                play_game(
                    self._game,
                    pair,
                    max_turns=self.state.max_turns,
                    number=number,
                    publish=self._publish,
                    backoff_s=self._backoff_s,
                )
            )
            try:
                result = await self._current
            except asyncio.CancelledError:
                if not self._stop_requested:
                    raise
                break
            finally:
                self._current = None
            self._record(result)
            if result.outcome is None:
                self.state.status, self.state.error = "aborted", result.error
                break
            self._publish(CompetitionProgress(self.state))

        if self.state.status == "running":
            self.state.status = "stopped" if self._stop_requested else "finished"
        self._save()
        self._publish(CompetitionFinished(self.state))
        return self.state

    def _record(self, result: GameResult) -> None:
        summary = GameSummary.of(result)
        self.state.results.append(summary)
        if result.outcome is not None:
            for name in result.players:
                self.state.stats[name].add(result)
        stem = self.run_dir / f"game-{result.number:03d}"
        stem.with_suffix(f".{self._game.record_extension}").write_text(
            self._game.record(result.state, result.outcome, result.players), encoding="utf-8"
        )
        game_json = {
            **dataclasses.asdict(summary),
            "side_names": self._game.side_names,
            "turns": [dataclasses.asdict(turn) for turn in result.turns],
        }
        _write_json(stem.with_suffix(".json"), game_json)
        self._save()

    def _save(self) -> None:
        state = dataclasses.asdict(self.state)
        state["prompt_version"] = self._prompt_version
        _write_json(self.run_dir / "competition.json", state)


def _safe(name: str) -> str:
    return re.sub(r"[^\w@.-]", "_", name)


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(run_dir: Path) -> CompetitionState:
    """Read a saved competition back, for `ollmpiada show`."""
    data = json.loads((run_dir / "competition.json").read_text(encoding="utf-8"))
    data.pop("prompt_version", None)
    data["side_names"], data["players"] = tuple(data["side_names"]), tuple(data["players"])
    data["stats"] = {name: PlayerStats(**s) for name, s in data["stats"].items()}
    data["results"] = [
        GameSummary(**{**r, "players": tuple(r["players"])}) for r in data["results"]
    ]
    return CompetitionState(**data)
