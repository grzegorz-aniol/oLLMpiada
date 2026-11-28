"""Inference servers of providers: started, stopped and freed before each experiment competition."""

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ollmpiada.config import Model, Provider

READY_TIMEOUT_S = 600.0
"""How long a server may take to list a model; loading a large model can take minutes."""
STOP_TIMEOUT_S = 30.0
"""How long a stop (and a `cleanup` command) may take before the runner moves on."""
POLL_S = 1.0
HTTP_TIMEOUT_S = 5.0
CLOCK_SLACK_S = 1.0
"""How much later than its PID file a group may seem to start and still be adopted, for the
coarse clocks of file times and process start times."""

log = logging.getLogger(__name__)


class ServerError(Exception):
    """A server that did not start or get ready, or a `cleanup` command that failed."""


@dataclass(frozen=True)
class Plan:
    """What preparing for one competition does, as provider names, in this order."""

    stop: tuple[str, ...]
    cleanup: tuple[str, ...]
    start: tuple[str, ...]


def plan(providers: Iterable[Provider], running: Iterable[str], used: set[str]) -> Plan:
    """The preparation for a competition whose players use the providers named `used`, while
    the owned servers named `running` are up."""
    providers = list(providers)
    running = list(running)
    return Plan(
        stop=tuple(name for name in running if name not in used),
        cleanup=tuple(p.name for p in providers if p.cleanup and p.name not in used),
        start=tuple(
            p.name for p in providers if p.start and p.name in used and p.name not in running
        ),
    )


def served_models(url: str) -> list[str] | None:
    """The model names the server lists at `url`, from `data[].id` (OpenAI shape) or
    `models[].name` (basal-serve); None when it does not answer, empty for any other shape."""
    try:
        response = httpx.get(url, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError, httpx.InvalidURL, ValueError:
        return None
    if not isinstance(data, dict):
        return []
    for key, field in (("data", "id"), ("models", "name")):
        items = data.get(key)
        if isinstance(items, list):
            return [
                item[field]
                for item in items
                if isinstance(item, dict) and isinstance(item.get(field), str)
            ]
    return []


def same_model(wanted: str, listed: str) -> bool:
    """Whether a listed name is the wanted model: either may end with the other, ignoring case,
    because proxies and servers prefix names differently (`llama.cpp/org/X` vs `org/X`)."""
    wanted, listed = wanted.lower(), listed.lower()
    return bool(listed) and (wanted.endswith(listed) or listed.endswith(wanted))


@dataclass
class _Server:
    """An owned server's process group; `process` is the group leader when this runner
    started it, None when it was adopted from a PID file."""

    pgid: int
    process: subprocess.Popen[bytes] | None = None

    def alive(self) -> bool:
        if self.process is not None:
            # Reaps an exited leader, whose zombie would otherwise keep the group signalable.
            self.process.poll()
        try:
            os.killpg(self.pgid, 0)
        except OSError:
            return False
        return True

    def signal(self, number: int) -> None:
        try:
            os.killpg(self.pgid, number)
        except ProcessLookupError:
            pass
        except OSError as e:
            # Logged, not raised, so close() still stops the other servers.
            log.warning("cannot signal process group %d: %s", self.pgid, e)


class Servers:
    """The servers of the providers in `models`, prepared competition by competition.

    Used as a context manager: entering adopts the owned servers still running from an
    earlier runner (their PID files in `pid_dir`); leaving stops every owned server and runs
    the `cleanup` of the providers the last competition used.
    """

    def __init__(
        self,
        models: dict[str, Model],
        pid_dir: Path,
        *,
        say: Callable[[str], None] = log.info,
        ready_timeout_s: float = READY_TIMEOUT_S,
        stop_timeout_s: float = STOP_TIMEOUT_S,
        poll_s: float = POLL_S,
    ) -> None:
        self._providers = {m.provider.name: m.provider for m in models.values()}
        self._pid_dir = pid_dir
        self._say = say
        self._ready_timeout_s = ready_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._poll_s = poll_s
        self._owned: dict[str, _Server] = {}
        self._used: set[str] = set()

    def __enter__(self) -> Servers:
        for provider in self._providers.values():
            if provider.start:
                self._adopt(provider.name)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def prepare(self, players: Iterable[Model]) -> None:
        """Make the servers ready for a competition of these players: stop the owned servers
        they do not use, run the `cleanup` of the providers they do not use, start their owned
        servers that are not up, and wait until each lists its players' models.

        Raises:
            ServerError: A cleanup failed, or a server did not start or get ready; an owned
                server that did not get ready is stopped.
        """
        players = list(players)
        self._used = {m.provider.name for m in players}
        steps = plan(self._providers.values(), self._owned, self._used)
        for name in steps.stop:
            self._stop(name)
        for name in steps.cleanup:
            self._cleanup(self._providers[name])
        for name in steps.start:
            self._start(self._providers[name])
        for name in sorted(self._used):
            provider = self._providers[name]
            if provider.models_url:
                wanted = [m.spec.model for m in players if m.provider.name == name]
                try:
                    self._wait_ready(provider, wanted)
                except ServerError:
                    if name in self._owned:
                        self._stop(name)
                    raise

    def close(self) -> None:
        """Stop every owned server and run the cleanups the last competition left; a failure
        is logged, so the rest still runs."""
        for name in list(self._owned):
            self._stop(name)
        for name in sorted(self._used):
            if self._providers[name].cleanup:
                try:
                    self._cleanup(self._providers[name])
                except ServerError as e:
                    log.warning("%s", e)
        self._used = set()

    def _pid_file(self, name: str) -> Path:
        return self._pid_dir / f"{name}.pid"

    def _log_file(self, name: str) -> Path:
        return self._pid_dir / f"{name}.log"

    def _adopt(self, name: str) -> None:
        path = self._pid_file(name)
        try:
            pgid = int(path.read_text())
            written = path.stat().st_mtime
        except OSError, ValueError:
            return
        # Group ids are reused, after a reboot or once PIDs wrap, so a group that started
        # after its PID file was written is an unrelated one.
        if not _group_started_by(pgid, written):
            path.unlink(missing_ok=True)
            return
        self._owned[name] = _Server(pgid)
        self._say(f"Adopted the running server of {name} (process group {pgid})")

    def _start(self, provider: Provider) -> None:
        assert provider.start
        self._pid_dir.mkdir(parents=True, exist_ok=True)
        output = self._log_file(provider.name)
        self._say(f"Starting the server of {provider.name} (output in {output})")
        with output.open("wb") as log_file:
            # A new session puts the whole tree (just, its script, the server and its
            # children) in one process group, which a stop signals at once.
            process = subprocess.Popen(
                provider.start,
                shell=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        self._owned[provider.name] = _Server(process.pid, process)
        self._pid_file(provider.name).write_text(str(process.pid))

    def _wait_ready(self, provider: Provider, wanted: list[str]) -> None:
        assert provider.models_url
        server = self._owned.get(provider.name)
        started = time.monotonic()
        while True:
            listed = served_models(provider.models_url)
            missing = [w for w in wanted if not any(same_model(w, n) for n in listed or ())]
            if not missing:
                self._say(f"{provider.name} is ready after {time.monotonic() - started:.0f} s")
                return
            if server is not None and not server.alive():
                raise ServerError(
                    f"the server of {provider.name} exited before it was ready; "
                    f"see {self._log_file(provider.name)}"
                )
            if time.monotonic() - started >= self._ready_timeout_s:
                reported = "no answer" if listed is None else ", ".join(listed) or "no models"
                raise ServerError(
                    f"{provider.name} did not list {', '.join(missing)} at "
                    f"{provider.models_url} within {self._ready_timeout_s:g} s; "
                    f"it reported: {reported}"
                )
            time.sleep(self._poll_s)

    def _stop(self, name: str) -> None:
        """SIGTERM to the group, SIGKILL when it outlives the stop timeout, then wait until
        `models_url` stops answering, so the GPU memory is free before the next start."""
        server = self._owned.pop(name)
        self._say(f"Stopping the server of {name}")
        if server.alive():
            server.signal(signal.SIGTERM)
            if not self._wait(lambda: not server.alive()):
                log.warning("%s did not stop within %g s; killing it", name, self._stop_timeout_s)
                server.signal(signal.SIGKILL)
                self._wait(lambda: not server.alive())
        url = self._providers[name].models_url
        if url and not self._wait(lambda: served_models(url) is None):
            log.warning("%s still answers after its server stopped", url)
        self._pid_file(name).unlink(missing_ok=True)

    def _cleanup(self, provider: Provider) -> None:
        assert provider.cleanup
        self._say(f"Running the cleanup of {provider.name}")
        try:
            result = subprocess.run(
                provider.cleanup,
                shell=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._stop_timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise ServerError(f"the cleanup of {provider.name} timed out") from e
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            raise ServerError(
                f"the cleanup of {provider.name} failed with exit code {result.returncode}: "
                f"{output}"
            )

    def _wait(self, done: Callable[[], bool]) -> bool:
        """Poll until `done` or the stop timeout; whether it got done."""
        deadline = time.monotonic() + self._stop_timeout_s
        while not done():
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_s)
        return True


def _group_started_by(pgid: int, written: float) -> bool:
    """Whether a running process of group `pgid` started no later than `written` (Unix time),
    read from /proc; never true off Linux, where nothing is adopted."""
    # Group 0 holds kernel threads, and signalling it would signal the runner's own group.
    if pgid < 1 or not hasattr(time, "CLOCK_BOOTTIME"):
        return False
    # Process start times in /proc count from boot, suspended time included.
    booted = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    ticks = os.sysconf("SC_CLK_TCK")
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            # Fields after the command name, which may hold spaces: state is field 3.
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        pgrp, start_ticks = int(fields[2]), int(fields[19])
        if pgrp == pgid and booted + start_ticks / ticks <= written + CLOCK_SLACK_S:
            return True
    return False
