"""Competition runner: a series of games between two players, with statistics and results."""

import asyncio
import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from ollmpiada.game_master import (
    TRANSPORT_BACKOFF_S,
    Event,
    GameMaster,
    GameResult,
    TurnBasedMaster,
)
from ollmpiada.games.base import Game, Side, first_full_move
from ollmpiada.players.base import Player, TransportError, TurnRequest, Usage
from ollmpiada.players.llm import answer_type, llm_message

Status = Literal["warming up", "running", "finished", "aborted", "stopped"]


@dataclass
class PlayerStats:
    """Totals for one player (model id plus effort) over its counted games. Each game counts
    in one of `wins`, `losses`, `draws` (decided on the board) or their adjudicated twins."""

    name: str
    games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    adjudicated_wins: int = 0
    adjudicated_losses: int = 0
    adjudicated_draws: int = 0
    moves: int = 0
    calls: int = 0
    thinking_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls_without_usage: int = 0
    calls_without_reasoning: int = 0
    illegal_attempts: int = 0
    points: int | None = None
    """The sum of the player's side's `Outcome.points`; None until a game with points counts."""

    @property
    def tokens_known(self) -> bool:
        return self.calls > self.calls_without_usage

    @property
    def reasoning_known(self) -> bool:
        return self.calls - self.calls_without_usage > self.calls_without_reasoning

    def add(self, result: GameResult) -> None:
        """Count one finished game."""
        outcome = result.outcome
        assert outcome is not None
        side = Side.FIRST if result.players[0] == self.name else Side.SECOND
        self.games += 1
        if outcome.winner is None:
            if outcome.adjudicated:
                self.adjudicated_draws += 1
            else:
                self.draws += 1
        elif outcome.winner is side:
            if outcome.adjudicated:
                self.adjudicated_wins += 1
            else:
                self.wins += 1
        elif outcome.adjudicated:
            self.adjudicated_losses += 1
        else:
            self.losses += 1
        if outcome.points is not None:
            self.points = (self.points or 0) + outcome.points[0 if side is Side.FIRST else 1]
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
    """The points in side order (`9-4`) for a game with points, else `1-0`, `0-1` or
    `1/2-1/2`; `aborted` for an aborted game."""
    reason: str
    moves: int
    score: str | None = None
    """What an adjudicated game was judged by (`Outcome.score`)."""
    points: tuple[int, int] | None = None
    """`Outcome.points`: what each side scored, in side order."""

    @classmethod
    def of(cls, result: GameResult) -> GameSummary:
        moves = sum(turn.action == "move" for turn in result.turns)
        outcome = result.outcome
        if outcome is None:
            return cls(result.number, result.players, None, "aborted", result.error or "", moves)
        winner, points = outcome.winner, outcome.points
        if points is None:
            scoreline = {Side.FIRST: "1-0", Side.SECOND: "0-1", None: "1/2-1/2"}[winner]
        else:
            scoreline = f"{points[0]}-{points[1]}"
        name = None if winner is None else result.players[0 if winner is Side.FIRST else 1]
        return cls(
            result.number,
            result.players,
            name,
            scoreline,
            outcome.reason,
            moves,
            outcome.score,
            points,
        )


@dataclass
class CompetitionState:
    """What the competition looks like now; every competition event carries it."""

    game: str
    side_names: tuple[str, str]
    players: tuple[str, str]
    games: int
    max_turns: int | None
    """None for a game without a turn limit (`Game.turn_limit`)."""
    seed: int | None
    """Game n starts from `game_seed(seed, n)`; None when read from a file without a seed."""
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


def start_number(number: int) -> int:
    """The start game `number` plays, counted from 1: games 2k-1 and 2k share start k, and
    sides alternate between them, so each pair plays the same start once from each side (a
    duplicate pair)."""
    return (number + 1) // 2


def game_seed(seed: int, number: int) -> int:
    """The seed of game `number`'s start; the first start takes `seed` itself."""
    return seed + start_number(number) - 1


def sample_requests(game: Game) -> dict[str, TurnRequest]:
    """The sample turns, by name, that `prompt_version` hashes and the golden test pins: the
    opening turn and a turn after the opening with a pending draw offer and a rejected attempt,
    so the wording of both answer formats, the draw offer and the retry is rendered."""
    master = TurnBasedMaster(game)
    later = first_full_move()
    max_turns = later if game.turn_limit else None
    return {
        "opening": master.start_request(max_turns=max_turns),
        "later": master.start_request(
            max_turns=max_turns, turn=later, draw_offered=True, rejection="-"
        ),
    }


def prompt_version(game: Game) -> str:
    """Hash of what an LLM reads in the sample turns: the message and the output schema of each,
    so it covers both templates, every wording branch in code, both LLM answer formats, the
    schema descriptions and the message layout."""
    # The answer format sits at the system/user boundary, so moving text between the parts
    # changes the hash.
    text = repr(
        tuple(
            (llm_message(r.prompt), json.dumps(answer_type(r.move_only).model_json_schema()))
            for r in sample_requests(game).values()
        )
    )
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class CompetitionSetupError(ValueError):
    """The competition's settings break one of its rules; the message says which."""


DEFAULT_MAX_TURNS = 50
"""Moves per side before adjudication when a competition of a game with a turn limit names
none."""


def default_max_turns(game: Game) -> int | None:
    return DEFAULT_MAX_TURNS if game.turn_limit else None


def check_games(game: Game, games: int) -> None:
    """Check the number of games against the game's rules.

    Raises:
        CompetitionSetupError: An odd number of games for a game played in duplicate pairs.
    """
    if games % 2 and game.duplicate:
        raise CompetitionSetupError(
            f"{game.name} plays every start twice, once from each side; "
            f"pick an even number of games, not {games}"
        )


class Competition:
    def __init__(
        self,
        master: GameMaster,
        players: tuple[Player, Player],
        *,
        games: int,
        max_turns: int | None,
        runs_dir: Path,
        seed: int = 0,
        publish: Callable[[CompetitionEvent], None] = lambda event: None,
        backoff_s: tuple[float, ...] = TRANSPORT_BACKOFF_S,
    ) -> None:
        self._master = master
        game = master.game
        self._players = players
        self._publish = publish
        self._backoff_s = backoff_s
        self._seed = seed
        self._current: asyncio.Task | None = None
        self._stop_requested = False
        names = (players[0].name, players[1].name)
        if names[0] == names[1]:
            raise CompetitionSetupError(
                f"both players are named {names[0]!r}; statistics need two names"
            )
        check_games(game, games)
        self.state = CompetitionState(
            game=game.name,
            side_names=game.side_names,
            players=names,
            games=games,
            max_turns=max_turns,
            seed=seed,
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
        self.state.status = "warming up"
        self._save()
        self._publish(CompetitionProgress(self.state))
        try:
            await self._until_stopped(self._warm_up())
        except TransportError as e:
            self.state.status, self.state.error = "aborted", str(e)
        if self.state.status == "warming up" and not self._stop_requested:
            self.state.status = "running"
            self._publish(CompetitionProgress(self.state))
            await self._play_games()

        if self.state.status in ("warming up", "running"):
            self.state.status = "stopped" if self._stop_requested else "finished"
        self._save()
        self._publish(CompetitionFinished(self.state))
        return self.state

    async def _warm_up(self) -> None:
        """Warm every player up; the one who moves first in game 1 goes last, so a server that
        keeps one model loaded at a time still holds it when the game starts.

        Raises:
            TransportError: A player failed the warm-up; the message names it.
        """
        game = self._master.game
        first_mover = game.side_to_move(game.new_state(game_seed(self._seed, 1)))
        order = self._players if first_mover is Side.SECOND else reversed(self._players)
        for player in order:
            try:
                await self._master.warm_up(
                    player, max_turns=self.state.max_turns, backoff_s=self._backoff_s
                )
            except TransportError as e:
                raise TransportError(f"warm-up call to {player.name} failed: {e}") from e

    async def _play_games(self) -> None:
        for number in range(1, self.state.games + 1):
            if self._stop_requested:
                return
            first, second = self._players
            pair = (first, second) if number % 2 == 1 else (second, first)
            result = await self._until_stopped(
                self._master.play(
                    pair,
                    number=number,
                    seed=game_seed(self._seed, number),
                    max_turns=self.state.max_turns,
                    publish=self._publish,
                    backoff_s=self._backoff_s,
                )
            )
            if result is None:
                return
            self._record(result)
            if result.outcome is None:
                self.state.status, self.state.error = "aborted", result.error
                return
            self._publish(CompetitionProgress(self.state))

    async def _until_stopped[T](self, step: Coroutine[object, object, T]) -> T | None:
        """Run `step` as a task that `stop()` cancels; None when it was stopped."""
        self._current = asyncio.create_task(step)
        try:
            return await self._current
        except asyncio.CancelledError:
            if not self._stop_requested:
                raise
            return None
        finally:
            self._current = None

    def _record(self, result: GameResult) -> None:
        summary = GameSummary.of(result)
        self.state.results.append(summary)
        if result.outcome is not None:
            for name in result.players:
                self.state.stats[name].add(result)
        game = self._master.game
        stem = self.run_dir / f"game-{result.number:03d}"
        stem.with_suffix(f".{game.record_extension}").write_text(
            game.record(result.state, result.outcome, result.players, start_number(result.number)),
            encoding="utf-8",
        )
        game_json = {
            **_as_json(summary),
            "side_names": game.side_names,
            "turns": [dataclasses.asdict(turn) for turn in result.turns],
        }
        _write_json(stem.with_suffix(".json"), game_json)
        self._save()

    def _save(self) -> None:
        state = _as_json(self.state)
        state["prompt_version"] = self._prompt_version
        _write_json(self.run_dir / "competition.json", state)


def _as_json(obj: object) -> dict:
    """`dataclasses.asdict` without `points` fields that are None, so a game without points
    (chess) saves no points at all."""
    return dataclasses.asdict(
        obj, dict_factory=lambda items: {k: v for k, v in items if k != "points" or v is not None}
    )


def _safe(name: str) -> str:
    return re.sub(r"[^\w@.-]", "_", name)


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(run_dir: Path) -> CompetitionState:
    """Read a saved competition back, for `ollmpiada show`."""
    data = json.loads((run_dir / "competition.json").read_text(encoding="utf-8"))
    data.pop("prompt_version", None)
    data.setdefault("seed", None)
    data["side_names"], data["players"] = tuple(data["side_names"]), tuple(data["players"])
    data["stats"] = {name: PlayerStats(**s) for name, s in data["stats"].items()}
    data["results"] = [_load_summary(r) for r in data["results"]]
    return CompetitionState(**data)


def _load_summary(data: dict) -> GameSummary:
    points = data.get("points")
    return GameSummary(
        **{
            **data,
            "players": tuple(data["players"]),
            "points": None if points is None else tuple(points),
        }
    )
