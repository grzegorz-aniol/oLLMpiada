import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ollmpiada.config import Model, ModelSpec, Provider
from ollmpiada.servers import ServerError, Servers, same_model, served_models
from tests.fakes import FakeServer, free_port, gone


def model(provider: str, served: str = "Org/Model-GGUF", **server_fields: str) -> Model:
    """A model of its own provider; `server_fields` are `start`, `models_url`, `cleanup`."""
    spec = ModelSpec(id=f"{provider}-model", provider=provider, model=served)
    served_by = Provider(
        name=provider, url="http://unused/v1", api="openai-completions", **server_fields
    )
    return Model(spec, served_by, None, True)


def models(*items: Model) -> dict[str, Model]:
    return {m.id: m for m in items}


@pytest.fixture
def pid_dir(tmp_path):
    """The PID file folder; groups still named there when a test ends are killed."""
    path = tmp_path / "services"
    yield path
    for pid_file in path.glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except OSError:
            pass


def servers(pid_dir: Path, *items: Model, said: list[str] | None = None, **timeouts) -> Servers:
    say = said.append if said is not None else lambda text: None
    options = {"ready_timeout_s": 5, "stop_timeout_s": 2, "poll_s": 0.05, **timeouts}
    return Servers(models(*items), pid_dir, say=say, **options)


@pytest.mark.parametrize(
    ("shape", "listed"),
    [("openai", "llama.cpp/org/model-gguf"), ("basal", "Model-GGUF")],
)
def test_start_waits_until_the_model_is_listed_and_stop_ends_the_whole_group(
    tmp_path, pid_dir, shape, listed
):
    fake = FakeServer(tmp_path, "basal", "--shape", shape, "--models", listed, "--delay", "0.3")
    basal = model("basal", start=fake.command, models_url=fake.url)
    with servers(pid_dir, basal) as s:
        s.prepare([basal])
        assert served_models(fake.url) == [listed]
        pgid = int((pid_dir / "basal.pid").read_text())
        child = fake.child
        assert os.getpgid(child) == pgid
    assert gone(child) and gone(pgid)
    assert served_models(fake.url) is None
    assert not (pid_dir / "basal.pid").exists()


def test_a_server_that_ignores_sigterm_is_killed(tmp_path, pid_dir, caplog):
    fake = FakeServer(tmp_path, "basal", "--models", "Org/Model-GGUF", "--ignore-term")
    basal = model("basal", start=fake.command, models_url=fake.url)
    with servers(pid_dir, basal, stop_timeout_s=0.5) as s:
        s.prepare([basal])
        child = fake.child
    assert gone(child)
    assert "did not stop within 0.5 s; killing it" in caplog.text


def test_the_timeout_error_lists_what_the_server_reported_and_stops_it(tmp_path, pid_dir):
    fake = FakeServer(tmp_path, "basal", "--models", "Other/Model", "Third")
    basal = model("basal", start=fake.command, models_url=fake.url)
    # Long enough for a loaded machine to start the fake server before the timeout.
    with servers(pid_dir, basal, ready_timeout_s=3) as s:
        with pytest.raises(ServerError) as error:
            s.prepare([basal])
        assert gone(fake.child)
    assert str(error.value) == (
        f"basal did not list Org/Model-GGUF at {fake.url} within 3 s; "
        "it reported: Other/Model, Third"
    )


def start_detached(fake: FakeServer) -> int:
    """Start a server the way a runner killed afterwards leaves it: a process group whose
    leader is gone; its group id once the server answers."""
    shell = subprocess.Popen(f"{fake.command} &", shell=True, start_new_session=True)
    shell.wait()
    deadline = time.monotonic() + 5
    while served_models(fake.url) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return shell.pid


@pytest.mark.parametrize(
    ("options", "reported"), [(("--shape", "other"), "no models"), (None, "no answer")]
)
def test_a_server_that_lists_nothing_readable_times_out(tmp_path, options, reported):
    # A server that is not owned is only waited for.
    url = f"http://127.0.0.1:{free_port()}/v1/models"
    pgid = None
    if options:
        fake = FakeServer(tmp_path, "router", *options)
        url = fake.url
        pgid = start_detached(fake)
    router = model("router", models_url=url)
    try:
        with servers(tmp_path, router, ready_timeout_s=0.5) as s:
            with pytest.raises(ServerError, match=f"it reported: {reported}$"):
                s.prepare([router])
    finally:
        if pgid:
            os.killpg(pgid, signal.SIGKILL)


def test_a_start_command_that_ends_fails_without_waiting(tmp_path, pid_dir):
    basal = model("basal", start="echo no GPU; exit 3", models_url="http://127.0.0.1:9/models")
    started = time.monotonic()
    with servers(pid_dir, basal, ready_timeout_s=30) as s:
        with pytest.raises(ServerError, match="exited before it was ready"):
            s.prepare([basal])
    assert time.monotonic() - started < 5
    assert (pid_dir / "basal.log").read_text() == "no GPU\n"


def test_a_group_from_a_pid_file_is_adopted_and_stopped(tmp_path, pid_dir):
    fake = FakeServer(tmp_path, "basal", "--models", "Org/Model-GGUF")
    basal = model("basal", start=fake.command, models_url=fake.url)
    other = model("other")
    pgid = start_detached(fake)
    pid_dir.mkdir()
    (pid_dir / "basal.pid").write_text(str(pgid))
    said: list[str] = []
    with servers(pid_dir, basal, other, said=said) as s:
        s.prepare([other])
        assert said == [
            f"Adopted the running server of basal (process group {pgid})",
            "Stopping the server of basal",
        ]
    assert gone(fake.child)
    assert not (pid_dir / "basal.pid").exists()


def test_a_pid_file_older_than_its_group_is_dropped_and_the_group_survives(tmp_path, pid_dir):
    # The group id was reused, by a reboot or by PIDs wrapping, for an unrelated group.
    fake = FakeServer(tmp_path, "basal", "--models", "Org/Model-GGUF")
    basal = model("basal", start=fake.command, models_url=fake.url)
    pgid = start_detached(fake)
    try:
        pid_dir.mkdir()
        (pid_dir / "basal.pid").write_text(str(pgid))
        written = time.time() - 60
        os.utime(pid_dir / "basal.pid", (written, written))
        with servers(pid_dir, basal) as s:
            s.prepare([])
        assert not (pid_dir / "basal.pid").exists()
        assert not gone(fake.child)
    finally:
        os.killpg(pgid, signal.SIGKILL)


@pytest.mark.parametrize("group", ["ended", "0"])
def test_a_pid_file_of_no_server_group_is_dropped(tmp_path, group):
    # Not the `pid_dir` fixture, which would kill group 0, the test's own, if the file stayed.
    if group == "ended":
        ended = subprocess.Popen(["true"], start_new_session=True)
        ended.wait()
        group = str(ended.pid)
    basal = model("basal", start="unused", models_url="http://127.0.0.1:9/models")
    (tmp_path / "basal.pid").write_text(group)
    said: list[str] = []
    with servers(tmp_path, basal, said=said):
        pass
    assert said == [] and not (tmp_path / "basal.pid").exists()


def test_preparation_order_across_competitions(tmp_path, pid_dir):
    first = FakeServer(tmp_path, "first", "--models", "Org/Model-GGUF")
    second = FakeServer(tmp_path, "second", "--models", "Org/Model-GGUF")
    cleanups = tmp_path / "cleanups.txt"
    a = model("a", start=first.command, models_url=first.url)
    b = model("b", start=second.command, models_url=second.url)
    router = model("router", cleanup=f"echo router >> {cleanups}")
    remote = model("remote")
    said: list[str] = []
    with servers(pid_dir, a, b, router, remote, said=said) as s:
        s.prepare([a, remote])
        s.prepare([a, b])
        s.prepare([router, b])
        a_child = first.child
        assert gone(a_child) and not gone(second.child)
    actions = [text.split(" (")[0] for text in said if "is ready" not in text]
    assert actions == [
        # a vs remote
        "Running the cleanup of router",
        "Starting the server of a",
        # a vs b: a stays up
        "Running the cleanup of router",
        "Starting the server of b",
        # router vs b: b stays up, and the router is cleaned up although it is used
        "Stopping the server of a",
        "Running the cleanup of router",
        # the end
        "Stopping the server of b",
        "Running the cleanup of router",
    ]
    assert [text for text in said if "is ready" in text][0].startswith("a is ready after")
    assert cleanups.read_text() == "router\n" * 4
    assert gone(second.child)


def reaped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_server_that_exited_between_competitions_is_started_again(tmp_path, pid_dir):
    fake = FakeServer(tmp_path, "basal", "--models", "Org/Model-GGUF")
    basal = model("basal", start=fake.command, models_url=fake.url)
    said: list[str] = []
    with servers(pid_dir, basal, said=said) as s:
        s.prepare([basal])
        crashed, child = int((pid_dir / "basal.pid").read_text()), fake.child
        os.killpg(crashed, signal.SIGKILL)  # as the kernel kills a server out of memory
        # The child's zombie, reaped by init, would keep the group signalable; the leader's
        # is reaped by the runner.
        deadline = time.monotonic() + 5
        while not (gone(crashed) and reaped(child)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gone(crashed) and reaped(child), "the killed server's processes outlived 5 s"
        s.prepare([basal])
        assert served_models(fake.url) == ["Org/Model-GGUF"]
        assert int((pid_dir / "basal.pid").read_text()) != crashed
    actions = [text.split(" (")[0] for text in said if "is ready" not in text]
    assert actions == [
        "Starting the server of basal",
        "The server of basal has exited",
        "Starting the server of basal",
        "Stopping the server of basal",
    ]


def test_a_server_that_cannot_be_started_fails_the_preparation(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    basal = model("basal", start="unused", models_url="http://127.0.0.1:9/models")
    with servers(blocker / "services", basal) as s:
        with pytest.raises(ServerError, match="^the server of basal did not start: "):
            s.prepare([basal])


def test_servers_are_stopped_and_cleaned_up_after_an_exception(tmp_path, pid_dir):
    fake = FakeServer(tmp_path, "basal", "--models", "Org/Model-GGUF")
    cleanups = tmp_path / "cleanups.txt"
    basal = model("basal", start=fake.command, models_url=fake.url)
    router = model("router", cleanup=f"echo router >> {cleanups}")
    with pytest.raises(RuntimeError, match="a bug"):
        with servers(pid_dir, basal, router) as s:
            s.prepare([basal, router])
            raise RuntimeError("a bug")
    assert gone(fake.child)
    assert cleanups.read_text() == "router\n" * 2  # before the competition and at the end


def test_a_failing_cleanup_fails_the_preparation(tmp_path, pid_dir):
    router = model("router", cleanup="echo out of luck >&2; exit 1")
    with servers(pid_dir, router, model("remote")) as s:
        with pytest.raises(ServerError, match="cleanup of router failed .* 1: out of luck"):
            s.prepare([model("remote")])


@pytest.mark.parametrize(
    ("wanted", "listed", "same"),
    [
        ("Remek/basal-2.0-max-GGUF", "basal-2.0-max-GGUF", True),
        ("llama.cpp/speakleash/X", "speakleash/x", True),
        ("org/model", "ORG/MODEL", True),
        ("org/model-a", "org/model-b", False),
        ("org/model", "", False),
    ],
)
def test_names_match_loosely(wanted, listed, same):
    assert same_model(wanted, listed) is same
