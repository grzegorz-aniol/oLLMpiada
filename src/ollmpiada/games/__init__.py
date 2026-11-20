"""Games by name, for `--game`."""

from ollmpiada.games.base import Game
from ollmpiada.games.chess_game import ChessGame

GAMES: dict[str, Game] = {"chess": ChessGame()}
