"""Game masters by game name, for `--game`.

It lives outside `games/` because `game_master` imports `games.base`, so a registry in
`games/__init__.py` would be an import cycle."""

from ollmpiada.game_master import GameMaster, TurnBasedMaster
from ollmpiada.games.bridge import BridgeGame
from ollmpiada.games.chess_game import ChessGame

MASTERS: dict[str, GameMaster] = {
    "chess": TurnBasedMaster(ChessGame()),
    "bridge": TurnBasedMaster(BridgeGame()),
}
