
import os
import io
import shutil
import requests
import chess
import chess.pgn
import chess.engine

from collections import Counter
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="Chess Scout Rival Analyzer")

LICHESS_API = "https://lichess.org/api"
DEPTH = 9


class AnalyzeRequest(BaseModel):
    username: str
    max_games: int = Field(default=10, ge=1, le=30)


def find_stockfish():
    configured = os.getenv("STOCKFISH_PATH")
    candidates = [
        configured,
        shutil.which("stockfish"),
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        r"C:\stockfish\stockfish-windows-x86-64-avx2.exe",
    ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path

    raise RuntimeError(
        "No encuentro Stockfish. Instalalo y configurá STOCKFISH_PATH."
    )


def score_cp(info, color):
    score = info["score"].pov(color)
    return score.score(mate_score=10000)


def category_for_drop(drop):
    if drop >= 300:
        return "Blunder"
    if drop >= 150:
        return "Mistake"
    if drop >= 80:
        return "Inaccuracy"
    return "Good"


def download_games(username, max_games):
    url = f"{LICHESS_API}/games/user/{username}"
    headers = {
        "Accept": "application/x-chess-pgn",
        "User-Agent": "ChessScout/1.0"
    }
    params = {
        "max": max_games,
        "moves": "true",
        "clocks": "false",
        "evals": "false",
        "opening": "true",
    }

    response = requests.get(
        url, headers=headers, params=params, timeout=60
    )

    if response.status_code == 404:
        raise HTTPException(404, "No se encontró ese usuario de Lichess.")
    if response.status_code == 429:
        raise HTTPException(429, "Lichess limitó temporalmente las consultas.")
    if response.status_code == 401:
        raise HTTPException(401, "No se pudieron consultar esas partidas.")
    if not response.ok:
        raise HTTPException(
            502, f"Lichess respondió con el código {response.status_code}."
        )

    stream = io.StringIO(response.text)
    games = []

    while len(games) < max_games:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        games.append(game)

    return games


def get_player_color(game, username):
    white = game.headers.get("White", "")
    black = game.headers.get("Black", "")

    if white.casefold() == username.casefold():
        return chess.WHITE
    if black.casefold() == username.casefold():
        return chess.BLACK
    return None


def get_result(game, color):
    result = game.headers.get("Result", "*")
    if result == "1/2-1/2":
        return "draw"
    if result == "*":
        return "unknown"
    if (result == "1-0" and color == chess.WHITE) or (
        result == "0-1" and color == chess.BLACK
    ):
        return "win"
    return "loss"


def analyze_game(game, username, engine):
    color = get_player_color(game, username)
    if color is None:
        return None

    board = game.board()
    moves = list(game.mainline_moves())
    player_moves = [
        (index, move)
        for index, move in enumerate(moves)
        if board.turn == color
        for board in []
    ]

    # Guardar los movimientos del rival y la posición previa a cada uno.
    board = game.board()
    positions = []
    for index, move in enumerate(moves):
        if board.turn == color:
            positions.append({
                "index": index,
                "board": board.copy(),
                "move": move,
                "san": board.san(move),
            })
        board.push(move)

    # Analizar como máximo 10 jugadas repartidas por partida.
    if len(positions) > 10:
        chosen = [
            positions[round(i * (len(positions) - 1) / 9)]
            for i in range(10)
        ]
        positions = list({
            item["index"]: item for item in chosen
        }.values())

    mistakes = []
    opening_counts = Counter()
    analyzed = 0

    for item in positions:
        before = item["board"]
        move = item["move"]
        player_color = color

        try:
            before_info = engine.analyse(
                before, chess.engine.Limit(depth=DEPTH)
            )
            best_move = before_info["pv"][0]
            before_score = score_cp(before_info, player_color)

            after = before.copy()
            after.push(move)
            after_info = engine.analyse(
                after, chess.engine.Limit(depth=DEPTH)
            )
            after_score = score_cp(after_info, player_color)
        except (chess.engine.EngineError, IndexError, KeyError):
            continue

        drop = before_score - after_score
        analyzed += 1

        if drop >= 80:
            mistakes.append({
                "move_number": item["index"] // 2 + 1,
                "played": item["san"],
                "best": before.san(best_move),
                "drop_cp": round(drop),
                "severity": category_for_drop(drop),
                "fen": before.fen(),
            })

    opening = (
        game.headers.get("Opening")
        or game.headers.get("ECO")
        or "Apertura no identificada"
    )

    return {
        "opponent": (
            game.headers.get("Black", "")
            if color == chess.WHITE
            else game.headers.get("White", "")
        ),
        "color": "white" if color == chess.WHITE else "black",
        "result": get_result(game, color),
        "opening": opening,
        "date": game.headers.get("Date", ""),
        "plies": len(moves),
        "analyzed_moves": analyzed,
        "mistakes": mistakes,
    }


@app.get("/")
def home():
    return FileResponse("index.html")


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest):
    username = request.username.strip()
    if not username:
        raise HTTPException(400, "Escribí el usuario de Lichess.")

    games = download_games(username, request.max_games)
    if not games:
        raise HTTPException(
            404, "No encontramos partidas públicas para analizar."
        )

    try:
        engine_path = find_stockfish()
        with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
            engine.configure({"Threads": 1, "Hash": 128})
            reports = []

            for game in games:
                report = analyze_game(game, username, engine)
                if report:
                    reports.append(report)

    except RuntimeError as error:
        raise HTTPException(503, str(error))
    except chess.engine.EngineError as error:
        raise HTTPException(503, f"Error al iniciar Stockfish: {error}")

    if not reports:
        raise HTTPException(
            404,
            "No pudimos vincular esas partidas con ese usuario. "
            "Comprobá el nombre."
        )

    all_mistakes = [
        {**mistake, "game": index + 1,
         "opening": report["opening"], "color": report["color"]}
        for index, report in enumerate(reports)
        for mistake in report["mistakes"]
    ]

    counts = Counter(m["severity"] for m in all_mistakes)
    openings = Counter(r["opening"] for r in reports)
    results = Counter(r["result"] for r in reports)
    colors = Counter(r["color"] for r in reports)

    recommendations = []
    if counts["Blunder"]:
        recommendations.append(
            "Buscá tácticas: el rival cometió errores graves en posiciones "
            "que Stockfish evaluó como desfavorables tras su jugada."
        )
    if counts["Mistake"]:
        recommendations.append(
            "Prepará posiciones complicadas: aparecen errores importantes "
            "que pueden permitirte ganar material o mejorar tu posición."
        )
    if counts["Inaccuracy"]:
        recommendations.append(
            "Priorizá jugadas sólidas y presión sostenida; se detectaron "
            "imprecisiones en las posiciones examinadas."
        )
    if not recommendations:
        recommendations.append(
            "No aparecieron pérdidas de evaluación grandes en la muestra. "
            "Analizá más partidas para conseguir un perfil más fiable."
        )

    return {
        "username": username,
        "games_found": len(reports),
        "moves_analyzed": sum(r["analyzed_moves"] for r in reports),
        "results": dict(results),
        "colors": dict(colors),
        "severity_counts": {
            "Blunders": counts["Blunder"],
            "Mistakes": counts["Mistake"],
            "Inaccuracies": counts["Inaccuracy"],
        },
        "common_openings": [
            {"name": name, "games": count}
            for name, count in openings.most_common(8)
        ],
        "weaknesses": sorted(
            all_mistakes,
            key=lambda m: m["drop_cp"],
            reverse=True
        )[:30],
        "games": reports,
        "recommendations": recommendations,
        "note": (
            "Análisis de una muestra de hasta 10 jugadas por partida, "
            "a profundidad 9. Las evaluaciones son aproximadas y no "
            "demuestran por sí solas una debilidad permanente."
        ),
    }
