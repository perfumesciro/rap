
import os
import logging
from collections import Counter
from typing import Optional

import requests
import chess
import chess.engine

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chess-scout")

app = FastAPI(title="Chess Scout API", version="2.0.0")

# Para GitHub Pages + Render.
# Podés definir ALLOWED_ORIGINS en Render como:
# https://TU-USUARIO.github.io,http://localhost:5500
origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = [
    origin.strip()
    for origin in origins_env.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

LICHESS_API = "https://lichess.org/api"
MAX_GAMES = 50
MAX_PLAYER_MOVES = 25

# Render suele instalar Stockfish en /usr/games/stockfish.
STOCKFISH_PATH = os.getenv(
    "STOCKFISH_PATH",
    "/usr/games/stockfish"
)

# Tiempo máximo aproximado por evaluación.
# Se usa un límite corto para evitar que el análisis tarde demasiado.
ENGINE_TIME = float(os.getenv("ENGINE_TIME", "0.025"))

HEADERS = {
    "User-Agent": "ChessScout/2.0 chess analysis application"
}


# --------------------------------------------------
# MODELO DE SOLICITUD
# --------------------------------------------------

class AnalyzeRequest(BaseModel):
    username: str = Field(min_length=1, max_length=30)
    max_games: int = Field(default=50, ge=1, le=50)
    max_moves_per_game: int = Field(default=25, ge=1, le=25)
    game_ids: Optional[list[str]] = Field(default=None, max_length=50)


# --------------------------------------------------
# RUTAS BÁSICAS
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "name": "Chess Scout API",
        "status": "online",
        "message": "Servidor funcionando correctamente."
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "stockfish_exists": os.path.isfile(STOCKFISH_PATH),
        "max_games": MAX_GAMES,
        "max_player_moves_per_game": MAX_PLAYER_MOVES
    }


# --------------------------------------------------
# DESCARGAR LAS ÚLTIMAS PARTIDAS DE LICHESS
# --------------------------------------------------

def download_games(username: str, limit: int) -> list[dict]:
    url = (
        f"{LICHESS_API}/games/user/"
        f"{requests.utils.quote(username, safe='')}"
    )

    params = {
        "max": min(limit, MAX_GAMES),
        "moves": "true",
        "opening": "true",
        "pgnInJson": "true"
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers={
                **HEADERS,
                "Accept": "application/x-ndjson"
            },
            timeout=(10, 35)
        )

        if response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="No se encontró ese usuario de Lichess."
            )

        if response.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Lichess está limitando las solicitudes. Esperá un poco e intentá nuevamente."
            )

        if not response.ok:
            raise HTTPException(
                status_code=502,
                detail=f"Lichess respondió con el error {response.status_code}."
            )

        games = []

        for line in response.text.splitlines():
            if not line.strip():
                continue

            try:
                game = requests.models.complexjson.loads(line)
                if isinstance(game, dict) and game.get("moves"):
                    games.append(game)
            except ValueError:
                logger.warning("Se ignoró una línea incompleta del historial.")

        # El endpoint de Lichess devuelve las partidas más recientes primero.
        return games[:limit]

    except HTTPException:
        raise
    except requests.RequestException as exc:
        logger.exception("Error conectando con Lichess")
        raise HTTPException(
            status_code=502,
            detail="No se pudo conectar con Lichess. Intentá nuevamente."
        ) from exc


# --------------------------------------------------
# IDENTIFICAR EL COLOR DEL JUGADOR
# --------------------------------------------------

def get_player_color(game: dict, username: str):
    players = game.get("players") or {}
    username_lower = username.lower()

    for color in ("white", "black"):
        player = players.get(color) or {}
        user = player.get("user") or {}

        names = [
            user.get("name"),
            user.get("id"),
            player.get("name")
        ]

        if any(
            isinstance(name, str) and name.lower() == username_lower
            for name in names
        ):
            return chess.WHITE if color == "white" else chess.BLACK

    return None


# --------------------------------------------------
# LEER LAS JUGADAS
# --------------------------------------------------

def parse_game_moves(game: dict) -> list[chess.Move]:
    """
    Intenta leer las jugadas como UCI.
    Si no funciona, intenta leerlas como SAN.
    """

    raw_moves = game.get("moves", "")

    if isinstance(raw_moves, list):
        tokens = [str(move) for move in raw_moves]
    else:
        tokens = str(raw_moves).split()

    board = chess.Board()
    moves = []

    for token in tokens:
        try:
            move = board.parse_uci(token)
        except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError):
            try:
                move = board.parse_san(token)
            except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError):
                # No se continúa con una partida si no podemos reconstruir
                # legalmente sus movimientos.
                return []

        moves.append(move)
        board.push(move)

        if board.is_game_over():
            break

    return moves


def get_phase(board: chess.Board) -> str:
    """Clasificación aproximada de la fase de la partida."""

    move_number = board.fullmove_number

    if move_number <= 10:
        return "Apertura"
    if move_number <= 25:
        return "Medio juego"
    return "Final"


# --------------------------------------------------
# ANALIZAR UNA PARTIDA CON STOCKFISH
# --------------------------------------------------

def analyze_one_game(
    engine: chess.engine.SimpleEngine,
    game: dict,
    username: str,
    max_player_moves: int
) -> dict:

    color = get_player_color(game, username)

    if color is None:
        return {
            "analyzed_moves": 0,
            "mistakes": [],
            "skipped": True
        }

    moves = parse_game_moves(game)

    if not moves:
        return {
            "analyzed_moves": 0,
            "mistakes": [],
            "skipped": True
        }

    board = chess.Board()
    mistakes = []
    analyzed_moves = 0
    player_moves_seen = 0

    game_id = game.get("id")

    for move in moves:
        if board.is_game_over():
            break

        is_player_move = board.turn == color

        if is_player_move and player_moves_seen >= max_player_moves:
            break

        if is_player_move:
            player_moves_seen += 1
            move_number = board.fullmove_number
            phase = get_phase(board)

            try:
                # Mejor evaluación disponible antes de la jugada.
                before_info = engine.analyse(
                    board,
                    chess.engine.Limit(time=ENGINE_TIME)
                )

                best_score = before_info["score"].pov(color).score(
                    mate_score=10000
                )

                # Guardamos la posición tras la jugada real.
                san_move = board.san(move)
                board.push(move)

                after_info = engine.analyse(
                    board,
                    chess.engine.Limit(time=ENGINE_TIME)
                )

                actual_score = after_info["score"].pov(color).score(
                    mate_score=10000
                )

                analyzed_moves += 1

                # Pérdida estimada en centipeones.
                # Los valores positivos indican que la jugada empeoró
                # la posición para el jugador analizado.
                loss = max(0, best_score - actual_score)

                if loss >= 50:
                    if loss >= 300:
                        error_type = "Error grave"
                    elif loss >= 100:
                        error_type = "Error importante"
                    else:
                        error_type = "Imprecisión"

                    mistakes.append({
                        "game_id": game_id,
                        "move_number": move_number,
                        "move": san_move,
                        "type": error_type,
                        "title": error_type,
                        "phase": phase,
                        "eval_loss": round(loss / 100, 2),
                        "description": (
                            f"En la jugada {move_number}, "
                            f"{san_move} perdió aproximadamente "
                            f"{loss / 100:.2f} peones de evaluación."
                        ),
                        "fen": board.fen()
                    })

            except (
                chess.engine.EngineError,
                chess.engine.EngineTerminatedError,
                ValueError
            ):
                logger.exception(
                    "Error evaluando una jugada de la partida %s",
                    game_id
                )

                # Si Stockfish falla, no inventamos una evaluación.
                # La partida se detiene para evitar resultados engañosos.
                break

        else:
            board.push(move)

    return {
        "analyzed_moves": analyzed_moves,
        "mistakes": mistakes,
        "skipped": False
    }


# --------------------------------------------------
# RESUMIR PATRONES DE DEBILIDAD
# --------------------------------------------------

def build_weaknesses(mistakes: list[dict]) -> list[dict]:
    if not mistakes:
        return [{
            "title": "No se detectaron errores importantes",
            "name": "Sin patrones confirmados",
            "count": 0,
            "occurrences": 0,
            "description": (
                "No se encontraron jugadas con una pérdida estimada "
                "de al menos 0.50 peones en las posiciones analizadas. "
                "Esto no significa que el jugador no tenga debilidades."
            )
        }]

    groups = Counter()
    phase_counts = Counter()

    for mistake in mistakes:
        groups[mistake["type"]] += 1
        phase_counts[mistake["phase"]] += 1

    weaknesses = []

    descriptions = {
        "Error grave": (
            "Conviene revisar estas posiciones: Stockfish detectó "
            "pérdidas de evaluación especialmente grandes."
        ),
        "Error importante": (
            "El jugador tuvo dificultades para mantener la evaluación "
            "en algunas posiciones."
        ),
        "Imprecisión": (
            "Hay jugadas que podrían mejorarse con una alternativa "
            "más precisa."
        )
    }

    for title, count in groups.most_common():
        weaknesses.append({
            "title": title,
            "name": title,
            "count": count,
            "occurrences": count,
            "description": descriptions.get(title, "")
        })

    for phase, count in phase_counts.most_common():
        weaknesses.append({
            "title": f"Errores en la fase de {phase.lower()}",
            "name": f"Fase: {phase}",
            "count": count,
            "occurrences": count,
            "description": (
                f"Se detectaron {count} jugadas con pérdidas estimadas "
                f"en esta fase. Revisá las partidas para comprobar "
                f"si existe un patrón repetido."
            )
        })

    return weaknesses


# --------------------------------------------------
# ENDPOINT PRINCIPAL
# --------------------------------------------------

@app.post("/api/analyze")
def analyze_player(payload: AnalyzeRequest):
    username = payload.username.strip()

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Tenés que indicar un usuario de Lichess."
        )

    if not os.path.isfile(STOCKFISH_PATH):
        logger.error("No se encuentra Stockfish en %s", STOCKFISH_PATH)
        raise HTTPException(
            status_code=503,
            detail=(
                "Stockfish no está instalado o no se encuentra en la ruta "
                "configurada. Revisá la instalación en Render."
            )
        )

    # Descargamos las partidas más recientes disponibles.
    games = download_games(username, payload.max_games)

    # Si el frontend envía los IDs de las últimas 50 partidas,
    # respetamos su orden y usamos únicamente esos IDs.
    if payload.game_ids:
        requested_ids = list(dict.fromkeys(payload.game_ids))[:MAX_GAMES]
        games_by_id = {
            game.get("id"): game
            for game in games
            if game.get("id")
        }

        ordered_games = [
            games_by_id[game_id]
            for game_id in requested_ids
            if game_id in games_by_id
        ]

        # Si no coinciden los IDs, seguimos con el historial reciente
        # descargado, en lugar de devolver un resultado vacío.
        if ordered_games:
            games = ordered_games

    games = games[:min(payload.max_games, MAX_GAMES)]

    if not games:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron partidas públicas para analizar."
        )

    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    except (OSError, chess.engine.EngineError) as exc:
        logger.exception("No se pudo iniciar Stockfish")
        raise HTTPException(
            status_code=503,
            detail="No se pudo iniciar Stockfish. Revisá la configuración del servidor."
        ) from exc

    all_mistakes = []
    total_moves = 0
    analyzed_games = 0
    skipped_games = 0
    colors = Counter()

    try:
        engine.configure({"Threads": 1})

        for index, game in enumerate(games):
            try:
                color = get_player_color(game, username)

                if color is not None:
                    colors["Blancas" if color == chess.WHITE else "Negras"] += 1

                result = analyze_one_game(
                    engine=engine,
                    game=game,
                    username=username,
                    max_player_moves=min(
                        payload.max_moves_per_game,
                        MAX_PLAYER_MOVES
                    )
                )

                if result["skipped"]:
                    skipped_games += 1
                    continue

                analyzed_games += 1
                total_moves += result["analyzed_moves"]
                all_mistakes.extend(result["mistakes"])

                logger.info(
                    "Partida %s/%s: %s jugadas analizadas",
                    index + 1,
                    len(games),
                    result["analyzed_moves"]
                )

            except Exception:
                # Una partida problemática no debe cancelar las demás.
                skipped_games += 1
                logger.exception(
                    "No se pudo analizar la partida %s",
                    game.get("id", "desconocida")
                )

    finally:
        try:
            engine.quit()
        except Exception:
            logger.exception("No se pudo cerrar Stockfish correctamente")

    return {
        "username": username,
        "requested_games": len(games),
        "analyzed_games": analyzed_games,
        "skipped_games": skipped_games,
        "max_moves_per_game": min(
            payload.max_moves_per_game,
            MAX_PLAYER_MOVES
        ),
        "analyzed_moves": total_moves,
        "mistake_count": len(all_mistakes),
        "color": (
            colors.most_common(1)[0][0]
            if len(colors) == 1
            else "Mixto"
        ),
        "weaknesses": build_weaknesses(all_mistakes),
        "mistakes": all_mistakes
    }
