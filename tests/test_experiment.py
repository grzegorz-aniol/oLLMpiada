import asyncio
import json
import logging
import os
import signal
from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest

from ollmpiada import cli
from ollmpiada.cli import main
from ollmpiada.competition import Competition
from ollmpiada.config import load_models
from ollmpiada.experiment import ExperimentError, load_experiment, progress
from ollmpiada.games.chess_game import ChessGame
from ollmpiada.players import resolve_player
from ollmpiada.players.base import TransportError, TurnAnswer
from ollmpiada.registry import MASTERS
from ollmpiada.servers import Servers
from tests.fakes import WARM_UP, FakeServer, ScriptedPlayer, gone

RESIGN = TurnAnswer(action="resign")
MODELS = {"L": "local-llm@high", "B": "basal-mini", "C": "claude@none"}


def write_experiment(root, id="exp", **fields):
    """An experiment folder; `fields` replace or add to a valid chess experiment of one
    competition."""
    data = {
        "game": "chess",
        "games": 2,
        "models": MODELS,
        "competitions": [{"players": ["L", "B"]}],
        **fields,
    }
    folder = root / id
    folder.mkdir(parents=True)
    (folder / "experiment.json").write_text(json.dumps(data))
    (folder / "README.md").write_text("Free text the runner never reads.")
    return folder


def write_run(folder, name, players, *, status="finished", game="chess", games=2, max_turns=50):
    """A run folder as the runner saves it, with only the fields that decide whether it
    counts."""
    run = folder / "runs" / name
    run.mkdir(parents=True)
    saved = {
        "game": game,
        "players": list(players),
        "games": games,
        "max_turns": max_turns,
        "status": status,
    }
    (run / "competition.json").write_text(json.dumps(saved))


@pytest.fixture
def root(tmp_path, monkeypatch, models_file):
    """The experiments root the CLI picks up, with run folders stamped one second apart."""
    path = tmp_path / "experiments"
    monkeypatch.setenv("OLLMPIADA_EXPERIMENTS_DIR", str(path))
    monkeypatch.setenv("COLUMNS", "200")
    clock = [datetime(2026, 11, 1)]

    class Ticking(datetime):
        @classmethod
        def now(cls, tz=None):
            clock[0] += timedelta(seconds=1)
            return clock[0]

    # Scripted competitions end within a second, and a run folder is named by the second.
    monkeypatch.setattr("ollmpiada.competition.datetime", Ticking)
    return path


@pytest.fixture
def models(models_file):
    return load_models(models_file)


@pytest.fixture
def played(monkeypatch):
    """Make the CLI use players that resign on every move, and record the competitions they
    play as (first, second) names. A name in `failing` raises on every call."""
    competitions: list[tuple[str, ...]] = []
    failing: set[str] = set()

    class Resigner(ScriptedPlayer):
        async def play(self, request):
            if self.name in failing:
                raise TransportError("connection refused")
            return await super().play(request)

    def fake_create(name, models, *, timeout_s):
        resolve_player(name, models)
        if not competitions or len(competitions[-1]) == 2:
            competitions.append(())
        competitions[-1] += (name,)
        return Resigner(name, [WARM_UP, RESIGN, RESIGN])

    monkeypatch.setattr("ollmpiada.cli.create_player", fake_create)
    monkeypatch.setattr(ChessGame, "session", lambda self, settings: nullcontext())
    monkeypatch.setitem(Competition.__init__.__kwdefaults__, "backoff_s", (0, 0))
    return competitions, failing


# --- experiment.json ---


def test_an_experiment_resolves_short_names_and_the_default_turn_limit(root, models):
    folder = write_experiment(
        root,
        competitions=[{"players": ["L", "B"]}, {"players": ["B", "L"], "runs": 2}],
    )
    experiment = load_experiment(folder, models)
    assert experiment.runs_dir == folder / "runs"
    assert (experiment.game, experiment.games, experiment.max_turns) == ("chess", 2, 50)
    assert [(p.players, p.runs) for p in experiment.competitions] == [
        (("local-llm@high", "basal-mini"), 1),
        (("basal-mini", "local-llm@high"), 2),
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"competitions": [{"players": ["L", "X"]}]}, "unknown short name 'X'"),
        ({"models": {**MODELS, "N": "nope"}}, "model 'N': unknown model 'nope'"),
        ({"models": {**MODELS, "L": "local-llm@medium"}}, "does not accept effort 'medium'"),
        (
            {"competitions": [{"players": ["L", "B"]}, {"players": ["L", "B"], "runs": 3}]},
            "competition local-llm@high vs basal-mini is listed twice",
        ),
        ({"competitions": [{"players": ["L", "B"], "runs": 0}]}, "greater than or equal to 1"),
        ({"game": "bridge", "games": 3}, "pick an even number of games, not 3"),
        ({"game": "bridge", "max_turns": 20}, "bridge has no turn limit; drop max_turns"),
        ({"game": "go"}, "unknown game 'go'"),
        ({"competitions": [{"players": ["B", "B"]}]}, "pick two different players"),
        ({"competitions": []}, "at least 1 item"),
        ({"competiton": []}, "Extra inputs are not permitted"),
    ],
)
def test_an_invalid_experiment_is_rejected_with_its_reason(root, models, fields, message):
    folder = write_experiment(root, **fields)
    with pytest.raises(ExperimentError, match="experiment.json") as error:
        load_experiment(folder, models)
    assert message in str(error.value)


def test_a_missing_experiment_is_rejected(root, models):
    with pytest.raises(ExperimentError, match="No such file"):
        load_experiment(root / "nothing", models)


# --- done is read from runs/ ---


def test_only_finished_runs_with_the_same_players_and_settings_count(root, models):
    folder = write_experiment(
        root, competitions=[{"players": ["L", "B"]}, {"players": ["L", "C"], "runs": 2}]
    )
    lb, bl = ("local-llm@high", "basal-mini"), ("basal-mini", "local-llm@high")
    lc = ("local-llm@high", "claude@none")
    write_run(folder, "01_stopped", lb, status="stopped")
    write_run(folder, "02_aborted", lb, status="aborted")
    write_run(folder, "03_swapped", bl)
    write_run(folder, "04_other_turn_limit", lb, max_turns=20)
    write_run(folder, "05_other_games", lb, games=4)
    write_run(folder, "06_other_game", lb, game="bridge", max_turns=None)
    write_run(folder, "07_lc", lc)
    (folder / "runs" / "08_unreadable").mkdir()
    rows = progress(load_experiment(folder, models))
    assert [run.path.name for run in rows[0].runs] == ["01_stopped", "02_aborted"]
    assert (rows[0].finished, rows[0].failed, rows[0].done) == (0, 2, False)
    assert (rows[1].finished, rows[1].done) == (1, False)

    write_run(folder, "09_lb", lb)
    write_run(folder, "10_lc", lc)
    rows = progress(load_experiment(folder, models))
    assert (rows[0].finished, rows[0].failed, rows[0].done) == (1, 2, True)
    assert (rows[1].finished, rows[1].done) == (2, True)


@pytest.mark.parametrize(
    "content",
    [
        b"[1, 2]",
        b'{"players": [["local-llm@high"], ["basal-mini"]], "game": "chess", "games": 2}',
        b'{"players": 5, "game": "chess", "games": 2}',
        b'{"game": "chess", "games": 2}',
        b'{"players": ["local-llm@high", "basal-mini"], "game": "\xff"}',
    ],
    ids=["not-an-object", "players-of-lists", "players-not-a-list", "no-players", "not-utf8"],
)
def test_a_run_folder_with_an_unreadable_competition_file_is_ignored(root, models, content):
    folder = write_experiment(root)
    (folder / "runs" / "01_odd").mkdir(parents=True)
    (folder / "runs" / "01_odd" / "competition.json").write_bytes(content)
    write_run(folder, "02_lb", ("local-llm@high", "basal-mini"))
    [row] = progress(load_experiment(folder, models))
    assert [run.path.name for run in row.runs] == ["02_lb"]


def test_runs_match_on_the_game_name_they_save_not_the_registry_key(root, models, monkeypatch):
    monkeypatch.setitem(MASTERS, "classic", MASTERS["chess"])
    folder = write_experiment(root, game="classic")
    write_run(folder, "01", ("local-llm@high", "basal-mini"), game="chess")
    assert progress(load_experiment(folder, models))[0].done


def test_status_shows_each_competition_with_its_runs(root, capsys):
    folder = write_experiment(
        root, competitions=[{"players": ["L", "B"]}, {"players": ["B", "L"], "runs": 2}]
    )
    write_run(folder, "01_a", ("local-llm@high", "basal-mini"), status="aborted")
    write_run(folder, "02_b", ("local-llm@high", "basal-mini"))
    assert main(["experiment", "status", "exp"]) == 0
    rows = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert "│ 1 │ local-llm@high vs basal-mini │ 1 │ 1 │ 1 │ 01_a (aborted) │" in rows
    assert "│ │ │ │ │ │ 02_b │" in rows
    assert "│ 2 │ basal-mini vs local-llm@high │ 2 │ 0 │ 0 │ - │" in rows


def test_status_of_an_invalid_experiment_exits_with_the_reason(root, capsys):
    write_experiment(root, games=0)
    assert main(["experiment", "status", "exp"]) == 2
    out = capsys.readouterr().out
    assert "Cannot load experiment" in out and "greater than or equal to 1" in out


def test_list_shows_each_experiment_with_its_done_competitions(root, capsys):
    folder = write_experiment(
        root, id="b-exp", competitions=[{"players": ["L", "B"]}, {"players": ["B", "L"]}]
    )
    write_run(folder, "01", ("basal-mini", "local-llm@high"))
    write_experiment(root, id="a-exp", game="go")
    assert main(["experiment", "list"]) == 0
    rows = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert "│ b-exp │ 1/2 │" in rows
    assert any(row.startswith("│ a-exp │ invalid:") and "unknown game" in row for row in rows)
    assert [row.split()[1] for row in rows if "-exp" in row] == ["a-exp", "b-exp"]


def test_list_without_experiments_says_so(root, capsys):
    assert main(["experiment", "list"]) == 0
    assert f"No experiments in {root}" in capsys.readouterr().out


# --- run ---


def test_run_plays_the_missing_runs_in_file_order_and_then_nothing(root, played, capsys):
    competitions, _ = played
    folder = write_experiment(
        root, competitions=[{"players": ["L", "B"], "runs": 2}, {"players": ["C", "L"]}]
    )
    write_run(folder, "20260101-000000_done", ("local-llm@high", "basal-mini"))
    assert main(["experiment", "run", "exp"]) == 0
    out = capsys.readouterr().out
    assert competitions == [("local-llm@high", "basal-mini"), ("claude@none", "local-llm@high")]
    assert "Competition 1 of 2: local-llm@high vs basal-mini, run 2 of 2" in out
    assert "Competition 2 of 2: claude@none vs local-llm@high, run 1 of 1" in out
    assert "2 of 2 competitions done." in out
    runs = sorted(path.name for path in (folder / "runs").iterdir())
    assert len(runs) == 3
    saved = json.loads((folder / "runs" / runs[-1] / "competition.json").read_text())
    assert saved["status"] == "finished" and saved["max_turns"] == 50

    assert main(["experiment", "run", "exp"]) == 0
    out = capsys.readouterr().out
    assert len(competitions) == 2 and len(list((folder / "runs").iterdir())) == 3
    assert "Nothing to play" in out and "2 of 2 competitions done." in out


def test_run_plays_every_wanted_run_of_a_competition(root, played, models):
    competitions, _ = played
    folder = write_experiment(root, competitions=[{"players": ["L", "B"], "runs": 2}])
    assert main(["experiment", "run", "exp"]) == 0
    assert len(competitions) == 2
    assert progress(load_experiment(folder, models))[0].finished == 2


@pytest.mark.parametrize("failure", ["aborted", "raised"])
def test_a_failing_competition_is_reported_and_the_next_one_plays(
    root, played, models, monkeypatch, capsys, caplog, failure
):
    competitions, failing = played
    folder = write_experiment(
        root,
        competitions=[
            {"players": ["L", "B"]},
            {"players": ["C", "B"], "runs": 2},
            {"players": ["B", "L"]},
        ],
    )
    if failure == "aborted":
        failing.add("claude@none")
    else:
        create = cli.create_player

        def broken(name, models, *, timeout_s):
            if name == "claude@none":
                raise RuntimeError("a bug")
            return create(name, models, timeout_s=timeout_s)

        monkeypatch.setattr("ollmpiada.cli.create_player", broken)
    with caplog.at_level(logging.ERROR):
        assert main(["experiment", "run", "exp"]) == 1
    out = capsys.readouterr().out
    # The failed competition is tried once, not once per wanted run.
    assert "Competition 2 of 3: claude@none vs basal-mini, run 1 of 2" in out
    assert "run 2 of 2" not in out
    assert "Competition 3 of 3: basal-mini vs local-llm@high, run 1 of 1" in out
    assert "2 of 3 competitions done; failed this time: 2." in out
    if failure == "raised":
        assert "competition claude@none vs basal-mini failed" in caplog.text
    rows = progress(load_experiment(folder, models))
    assert [row.done for row in rows] == [True, False, True]
    assert competitions[-1] == ("basal-mini", "local-llm@high")


def test_ctrl_c_stops_the_experiment_after_the_competition_in_progress(
    root, played, monkeypatch, capsys
):
    competitions, _ = played
    folder = write_experiment(root, competitions=[{"players": ["L", "B"]}, {"players": ["B", "L"]}])
    play = ScriptedPlayer.play

    async def press_ctrl_c_in_the_first_game(self, request):
        if len(self.requests) == 1:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(10)
        return await play(self, request)

    monkeypatch.setattr(ScriptedPlayer, "play", press_ctrl_c_in_the_first_game)
    assert main(["experiment", "run", "exp"]) == 1
    out = capsys.readouterr().out
    assert "Competition stopped" in out and "Competition 2 of 2" not in out
    assert "0 of 2 competitions done." in out
    assert len(competitions) == 1
    [run] = (folder / "runs").iterdir()
    assert json.loads((run / "competition.json").read_text())["status"] == "stopped"


# --- servers ---


def add_server_fields(models_file, **providers):
    """Give the named providers of the model config their `start`, `models_url`, `cleanup`."""
    data = json.loads(models_file.read_text())
    for provider in data["providers"]:
        provider.update(providers.get(provider["name"], {}))
    models_file.write_text(json.dumps(data))


@pytest.fixture
def quick_servers(monkeypatch):
    for name, value in (("ready_timeout_s", 5), ("stop_timeout_s", 2), ("poll_s", 0.05)):
        monkeypatch.setitem(Servers.__init__.__kwdefaults__, name, value)


def test_run_prepares_the_servers_before_each_competition_and_stops_them_at_the_end(
    root, played, models_file, tmp_path, quick_servers, capsys
):
    competitions, _ = played
    fake = FakeServer(tmp_path, "local", "--models", "Qwen")
    cleanups = tmp_path / "cleanups.txt"
    add_server_fields(
        models_file,
        local={"start": fake.command, "models_url": fake.url},
        **{"claude-proxy": {"cleanup": f"echo claude-proxy >> {cleanups}"}},
    )
    write_experiment(root, competitions=[{"players": ["L", "B"]}, {"players": ["C", "B"]}])
    assert main(["experiment", "run", "exp"]) == 0
    out = capsys.readouterr().out
    steps = [
        "Running the cleanup of claude-proxy",
        "Starting the server of local",
        "local is ready after",
        "Competition 1 of 2: local-llm@high vs basal-mini, run 1 of 1",
        "Stopping the server of local",
        "Running the cleanup of claude-proxy",
        "Competition 2 of 2: claude@none vs basal-mini, run 1 of 1",
        "Running the cleanup of claude-proxy",
        "2 of 2 competitions done.",
    ]
    position = 0
    for step in steps:  # in this order
        position = out.index(step, position) + len(step)
    assert cleanups.read_text() == "claude-proxy\n" * 3
    assert len(competitions) == 2 and gone(fake.child)


def test_a_competition_whose_server_fails_is_reported_and_the_next_one_plays(
    root, played, models_file, quick_servers, capsys
):
    competitions, _ = played
    add_server_fields(
        models_file, local={"start": "exit 3", "models_url": "http://127.0.0.1:9/models"}
    )
    write_experiment(root, competitions=[{"players": ["L", "B"]}, {"players": ["C", "B"]}])
    assert main(["experiment", "run", "exp"]) == 1
    out = capsys.readouterr().out
    assert "Competition 1: the server of local exited before it was ready" in out
    assert "Competition 1 of 2" not in out
    assert "1 of 2 competitions done; failed this time: 1." in out
    assert competitions == [("claude@none", "basal-mini")]


def test_ctrl_c_while_a_server_gets_ready_stops_it_and_ends_the_experiment(
    root, played, models_file, tmp_path, quick_servers, monkeypatch, capsys
):
    competitions, _ = played
    fake = FakeServer(tmp_path, "local", "--models", "Qwen", "--delay", "30")
    add_server_fields(models_file, local={"start": fake.command, "models_url": fake.url})
    write_experiment(root, competitions=[{"players": ["L", "B"]}, {"players": ["C", "B"]}])
    pid_file = tmp_path / "services" / "local.pid"
    groups: list[int] = []

    def press_ctrl_c(self, provider, wanted):
        groups.append(int(pid_file.read_text()))
        raise KeyboardInterrupt

    monkeypatch.setattr(Servers, "_wait_ready", press_ctrl_c)
    assert main(["experiment", "run", "exp"]) == 1
    out = capsys.readouterr().out
    assert "Stopping the server of local" in out and "Experiment stopped." in out
    assert "0 of 2 competitions done." in out
    assert competitions == [] and not pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(groups[0], 0)


def test_dry_run_prints_the_preparation_and_touches_nothing(
    root, played, models_file, tmp_path, monkeypatch, capsys
):
    competitions, _ = played
    marker = tmp_path / "touched"
    url = "http://127.0.0.1:9/models"
    add_server_fields(
        models_file,
        local={"start": f"touch {marker}", "models_url": url},
        basal={"start": f"touch {marker}", "models_url": url},
        **{"claude-proxy": {"cleanup": f"touch {marker}"}},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run starts nothing and asks nothing")

    for name in ("subprocess.Popen", "subprocess.run", "httpx.get"):
        monkeypatch.setattr(f"ollmpiada.servers.{name}", forbidden)
    folder = write_experiment(
        root,
        models={**MODELS, "H": "hosted-llm"},
        competitions=[{"players": ["L", "B"]}, {"players": ["H", "B"]}, {"players": ["C", "L"]}],
    )
    write_run(folder, "01", ("local-llm@high", "basal-mini"))
    assert main(["experiment", "run", "exp", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Competition 2 of 3: hosted-llm vs basal-mini",
        "  stop: -",
        "  cleanup: claude-proxy",
        "  start: basal",
        "Competition 3 of 3: claude@none vs local-llm@high",
        "  stop: basal",
        "  cleanup: claude-proxy",
        "  start: local",
        "At the end:",
        "  stop: local",
        "  cleanup: claude-proxy",
    ]
    assert not marker.exists() and not (tmp_path / "services").exists()
    assert competitions == [] and len(list((folder / "runs").iterdir())) == 1
