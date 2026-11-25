---
related: ["README.md", "architecture.md", "game-master/README.md"]
---

# Product

[Documentation index](README.md)

## What it is

An experiment that lets AI models play board games against each other. The user
picks two models, a game and the number of games; a game master referees every
move and the app shows live results and statistics. The games are chess and
contract bridge card play.

## Who it is for

The author, comparing how well different models (local, proxied or hosted;
generative LLMs and decision models) play games under identical conditions.

## Scope

A competition is started with one terminal command (`ollmpiada run`, see
[architecture](architecture.md#cli-and-terminal-ui)) that takes:

- two models from the [model config](configuration.md), each with a reasoning
  effort the model supports;
- the game, `chess` or `bridge` (contract bridge card play), and the number of
  games; what each game plays and leaves out is listed under
  [supported games](../README.md#supported-games);
- the maximum number of turns per game (chess only);
- a seed that fixes the starts (the bridge deals), so a run can be replayed;
- the mode: **visual** shows every move on a board as it happens, **background**
  plays without the board.

In both modes the terminal shows live status: games done out of total, the result
of each finished game and per-player statistics:

| Statistic | Meaning |
| --- | --- |
| Games | Finished games the player took part in |
| Wins / losses / draws | Per player, with the reason each game ended; games adjudicated at the turn limit are counted apart |
| Points | In bridge, the tricks the player's pair took over all its tables |
| Thinking time | Total and per-move wall-clock time of the model calls |
| Tokens | Input, output and reasoning tokens, when the provider reports them |
| Illegal attempts | Responses the game master rejected |

The rules of refereeing, prompting and scoring are owned by
[game master](game-master/README.md).

- **Decision:** the app has a terminal UI only. One command starts a competition
  with nothing else to run, and the same command can be scripted.
- **Decision:** one competition runs at a time, its games one after another.
  Parallel games are a later option, not part of the MVP.
- **Decision:** results are written to disk per competition (`.local/runs/`), so a
  finished competition can be inspected after a restart. Leaderboards across
  competitions are not part of the MVP.

## Non-goals

- Production quality: no authentication, no multi-user support, no database.
- Polished UX: the UI is simple and clean, nothing more.
- Chess engines or humans as players. A player is always a configured model.
  Stockfish only referees: it judges the final position of a game that reaches
  the turn limit (see [end of game](game-master/results.md#end-of-game)).

## Stack

- **Python 3.14 with uv** - one language for the referee, the model calls and the UI.
- **PydanticAI** (`pydantic-ai-slim[anthropic,openai]`) - LLM calls with structured output,
  usage counting and per-call reasoning effort; already used in the author's other
  projects, and leaves room for agent-style players with tools later.
- **httpx** - calls to decision models, which speak the SystemOne API rather than
  a chat API.
- **chess** (python-chess) - move generation, validation, end-of-game detection,
  PGN export and the UCI client that talks to Stockfish.
- **Stockfish** (installed separately) - judges positions at the turn limit.
- **Rich** - the terminal UI: a live-updating view with tables and a colored board.
- **argparse** (standard library) - command-line subcommands and flags.
- **python-dotenv** - loads provider tokens from `.env` at startup.
