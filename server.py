import os
import io
import requests
import chess
import chess.pgn
import chess.engine

from collections import Counter, defaultdict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# CONFIGURACIÓN
# =========================================================

app = FastAPI(title="Chess Scout API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LICHESS = "https://lichess.org"
MAX_PARTIDAS = 500

# Podés cambiar esto en Render mediante una variable de entorno.
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "stockfish")

# Tiempo aproximado que Stockfish piensa por jugada.
STOCKFISH_TIME = float(os.getenv("STOCKFISH_TIME", "0.5"))


# =========================================================
# STOCKFISH
# =========================================================

engine = None


def obtener_engine():
    global engine

    if engine is not None:
        return engine

    posibles_rutas = [
        STOCKFISH_PATH,
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish"
    ]

    for ruta in posibles_rutas:
        try:
            engine = chess.engine.SimpleEngine.popen_uci(ruta)

            try:
                engine.configure({
                    "Threads": 2,
                    "Hash": 128
                })
            except Exception:
                pass

            print("Stockfish iniciado:", ruta)
            return engine

        except Exception:
            continue

    raise RuntimeError(
        "No se encontró Stockfish. "
        "Instalá Stockfish o configurá STOCKFISH_PATH."
    )


# =========================================================
# MODELOS
# =========================================================

class NuevaBatallaRequest(BaseModel):
    usuario: str
    color: str = "white"


class MovimientoRequest(BaseModel):
    batalla_id: str
    movimiento: str


# =========================================================
# DATOS EN MEMORIA
# =========================================================

perfiles_cache = {}
batallas = {}


# =========================================================
# DESCARGAR PARTIDAS DE LICHESS
# =========================================================

def descargar_partidas(usuario):

    url = f"{LICHESS}/api/games/user/{usuario}"

    params = {
        "max": MAX_PARTIDAS,
        "moves": "true",
        "tags": "true",
        "clocks": "false",
        "evals": "false",
        "opening": "true"
    }

    headers = {
        "Accept": "application/x-chess-pgn"
    }

    respuesta = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=30
    )

    if respuesta.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="No se encontró ese jugador en Lichess."
        )

    if respuesta.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail="No se pudieron descargar las partidas de Lichess."
        )

    return respuesta.text


# =========================================================
# POSICIÓN NORMALIZADA
# =========================================================

def clave_posicion(board):

    # Usamos solamente los primeros 4 campos del FEN.
    # Así no importan los contadores de medio movimiento
    # ni el número de jugada.
    return " ".join(board.fen().split(" ")[:4])


# =========================================================
# CONSTRUIR EL ESTILO DEL JUGADOR
# =========================================================

def construir_perfil(usuario):

    usuario = usuario.strip()

    if not usuario:
        raise HTTPException(
            status_code=400,
            detail="Tenés que escribir un usuario de Lichess."
        )

    if usuario.lower() in perfiles_cache:
        return perfiles_cache[usuario.lower()]

    pgn_text = descargar_partidas(usuario)

    archivo = io.StringIO(pgn_text)

    partidas = []

    while True:

        partida = chess.pgn.read_game(archivo)

        if partida is None:
            break

        partidas.append(partida)

        if len(partidas) >= MAX_PARTIDAS:
            break

    if not partidas:
        raise HTTPException(
            status_code=404,
            detail="El jugador no tiene partidas públicas disponibles."
        )

    # -----------------------------------------------------
    # Averiguar color del jugador en cada partida
    # -----------------------------------------------------

    movimientos_por_posicion = {
        "white": defaultdict(Counter),
        "black": defaultdict(Counter)
    }

    # Repertorio por secuencia de movimientos propios.
    #
    # Ejemplo:
    #
    # jugador:
    # 1. e4
    # 2. Cf3
    # 3. Ab5
    #
    # Guardamos:
    #
    # () -> e4
    # (e4) -> Cf3
    # (e4, Cf3) -> Ab5
    #
    # Esto permite continuar con su estilo aunque
    # el rival juegue diferente.
    repertorio = {
        "white": defaultdict(Counter),
        "black": defaultdict(Counter)
    }

    movimientos_por_numero = {
        "white": defaultdict(Counter),
        "black": defaultdict(Counter)
    }

    cantidad_blancas = 0
    cantidad_negras = 0

    for partida in partidas:

        headers = partida.headers

        white = headers.get("White", "").lower()
        black = headers.get("Black", "").lower()

        if usuario.lower() == white:
            color = "white"
            cantidad_blancas += 1

        elif usuario.lower() == black:
            color = "black"
            cantidad_negras += 1

        else:
            continue

        board = partida.board()

        propios = []

        for movimiento in partida.mainline_moves():

            # Es el turno del jugador buscado
            if (
                (color == "white" and board.turn == chess.WHITE)
                or
                (color == "black" and board.turn == chess.BLACK)
            ):

                numero_jugada = len(propios) + 1

                # Solo las primeras 7 jugadas DEL JUGADOR.
                if numero_jugada <= 7:

                    key = clave_posicion(board)

                    uci = movimiento.uci()

                    movimientos_por_posicion[color][key][uci] += 1

                    movimientos_por_numero[color][numero_jugada][uci] += 1

                    # Guardar según las jugadas propias anteriores.
                    prefijo = tuple(propios)

                    repertorio[color][prefijo][uci] += 1

                    propios.append(uci)

            board.push(movimiento)

    perfil = {
        "usuario": usuario,
        "partidas": len(partidas),
        "partidas_blancas": cantidad_blancas,
        "partidas_negras": cantidad_negras,
        "movimientos_por_posicion": movimientos_por_posicion,
        "movimientos_por_numero": movimientos_por_numero,
        "repertorio": repertorio
    }

    perfiles_cache[usuario.lower()] = perfil

    return perfil


# =========================================================
# ELEGIR UNA DE LAS PRIMERAS 7 JUGADAS
# =========================================================

def elegir_jugada_repertorio(
    perfil,
    board,
    color,
    movimientos_realizados
):

    movimientos_por_posicion = perfil["movimientos_por_posicion"][color]

    repertorio = perfil["repertorio"][color]

    movimientos_por_numero = perfil["movimientos_por_numero"][color]

    # -----------------------------------------------------
    # OPCIÓN 1
    # Misma posición exacta de una partida real
    # -----------------------------------------------------

    key = clave_posicion(board)

    if key in movimientos_por_posicion:

        candidatos = movimientos_por_posicion[key]

        legales = []

        for uci, cantidad in candidatos.items():

            try:
                movimiento = chess.Move.from_uci(uci)

                if movimiento in board.legal_moves:
                    legales.append((uci, cantidad))

            except Exception:
                pass

        if legales:

            legales.sort(
                key=lambda x: x[1],
                reverse=True
            )

            return chess.Move.from_uci(
                legales[0][0]
            )


    # -----------------------------------------------------
    # OPCIÓN 2
    # Buscar la siguiente jugada según las jugadas propias
    # que ya hizo la IA.
    # -----------------------------------------------------

    prefijo = tuple(movimientos_realizados)

    if prefijo in repertorio:

        candidatos = repertorio[prefijo]

        legales = []

        for uci, cantidad in candidatos.items():

            try:
                movimiento = chess.Move.from_uci(uci)

                if movimiento in board.legal_moves:
                    legales.append((uci, cantidad))

            except Exception:
                pass

        if legales:

            legales.sort(
                key=lambda x: x[1],
                reverse=True
            )

            return chess.Move.from_uci(
                legales[0][0]
            )


    # -----------------------------------------------------
    # OPCIÓN 3
    # Buscar una jugada típica del jugador para ese número
    # -----------------------------------------------------

    numero = len(movimientos_realizados) + 1

    candidatos = movimientos_por_numero[color].get(
        numero,
        Counter()
    )

    legales = []

    for uci, cantidad in candidatos.items():

        try:
            movimiento = chess.Move.from_uci(uci)

            if movimiento in board.legal_moves:
                legales.append((uci, cantidad))

        except Exception:
            pass

    if legales:

        legales.sort(
            key=lambda x: x[1],
            reverse=True
        )

        return chess.Move.from_uci(
            legales[0][0]
        )


    # -----------------------------------------------------
    # OPCIÓN 4
    # Si ninguna de las jugadas reales es posible,
    # usamos Stockfish para no hacer una jugada ilegal.
    # -----------------------------------------------------

    return None


# =========================================================
# STOCKFISH JUGADA
# =========================================================

def elegir_jugada_stockfish(board):

    if board.is_game_over():
        return None

    motor = obtener_engine()

    resultado = motor.play(
        board,
        chess.engine.Limit(
            time=STOCKFISH_TIME
        )
    )

    return resultado.move


# =========================================================
# CREAR BATALLA
# =========================================================

@app.post("/api/batalla/iniciar")
def iniciar_batalla(datos: NuevaBatallaRequest):

    usuario = datos.usuario.strip()

    color_usuario = datos.color.lower()

    if color_usuario not in ["white", "black"]:
        color_usuario = "white"

    perfil = construir_perfil(usuario)

    # El jugador buscado será siempre la IA.
    #
    # Si nosotros jugamos blancas:
    # IA = negras
    #
    # Si nosotros jugamos negras:
    # IA = blancas

    if color_usuario == "white":
        color_ia = "black"
    else:
        color_ia = "white"

    import uuid

    batalla_id = str(uuid.uuid4())

    board = chess.Board()

    batalla = {
        "id": batalla_id,
        "usuario": usuario,
        "perfil": perfil,
        "board": board,
        "color_usuario": color_usuario,
        "color_ia": color_ia,

        # Jugadas propias del jugador buscado
        # que la IA ya reprodujo.
        "movimientos_ia": [],

        "terminada": False
    }

    batallas[batalla_id] = batalla

    respuesta = {
        "batalla_id": batalla_id,
        "usuario": usuario,
        "color_usuario": color_usuario,
        "color_ia": color_ia,
        "fen": board.fen(),
        "jugada_ia": None,
        "san": None,
        "movimientos_ia": 0,
        "modo_ia": "repertorio"
    }

    # -----------------------------------------------------
    # Si el usuario eligió negras,
    # la IA tiene que empezar.
    # -----------------------------------------------------

    if color_usuario == "black":

        movimiento = elegir_jugada_repertorio(
            perfil,
            board,
            color_ia,
            batalla["movimientos_ia"]
        )

        modo = "repertorio"

        # Si no encontró una jugada real legal,
        # Stockfish resuelve la posición.
        if movimiento is None:
            movimiento = elegir_jugada_stockfish(board)
            modo = "stockfish"

        if movimiento is None:
            raise HTTPException(
                status_code=500,
                detail="La IA no pudo encontrar una jugada."
            )

        san = board.san(movimiento)

        board.push(movimiento)

        # Solo contamos la jugada como una de las primeras
        # 7 si fue elegida del repertorio real.
        if modo == "repertorio":
            batalla["movimientos_ia"].append(
                movimiento.uci()
            )

        respuesta["jugada_ia"] = movimiento.uci()
        respuesta["san"] = san
        respuesta["fen"] = board.fen()
        respuesta["movimientos_ia"] = len(
            batalla["movimientos_ia"]
        )
        respuesta["modo_ia"] = modo

    return respuesta


# =========================================================
# RECIBIR JUGADA DEL USUARIO
# =========================================================

@app.post("/api/batalla/movimiento")
def realizar_movimiento(datos: MovimientoRequest):

    if datos.batalla_id not in batallas:
        raise HTTPException(
            status_code=404,
            detail="La batalla no existe."
        )

    batalla = batallas[datos.batalla_id]

    board = batalla["board"]

    if batalla["terminada"]:
        raise HTTPException(
            status_code=400,
            detail="La partida ya terminó."
        )

    # -----------------------------------------------------
    # JUGADA DEL USUARIO
    # -----------------------------------------------------

    try:

        movimiento_usuario = chess.Move.from_uci(
            datos.movimiento
        )

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Movimiento inválido."
        )

    if movimiento_usuario not in board.legal_moves:
        raise HTTPException(
            status_code=400,
            detail="Ese movimiento no es legal."
        )

    san_usuario = board.san(
        movimiento_usuario
    )

    board.push(movimiento_usuario)

    # -----------------------------------------------------
    # ¿TERMINÓ LA PARTIDA?
    # -----------------------------------------------------

    if board.is_game_over():

        batalla["terminada"] = True

        return {
            "ok": True,
            "san_usuario": san_usuario,
            "jugada_ia": None,
            "san_ia": None,
            "fen": board.fen(),
            "terminada": True,
            "resultado": board.result(),
            "movimientos_ia": len(
                batalla["movimientos_ia"]
            ),
            "modo_ia": None
        }

    # -----------------------------------------------------
    # JUGADA DE LA IA
    # -----------------------------------------------------

    perfil = batalla["perfil"]

    color_ia = batalla["color_ia"]

    movimientos_ia = batalla["movimientos_ia"]

    # -----------------------------------------------------
    # PRIMERAS 7 JUGADAS DEL JUGADOR
    # -----------------------------------------------------

    if len(movimientos_ia) < 7:

        movimiento_ia = elegir_jugada_repertorio(
            perfil,
            board,
            color_ia,
            movimientos_ia
        )

        modo_ia = "repertorio"

        # Si no existe una continuación legal de sus
        # partidas, Stockfish evita una jugada absurda.
        if movimiento_ia is None:

            movimiento_ia = elegir_jugada_stockfish(
                board
            )

            modo_ia = "stockfish"

    else:

        # -------------------------------------------------
        # DESPUÉS DE 7:
        # STOCKFISH COMPLETAMENTE
        # -------------------------------------------------

        movimiento_ia = elegir_jugada_stockfish(
            board
        )

        modo_ia = "stockfish"

    if movimiento_ia is None:

        batalla["terminada"] = True

        return {
            "ok": True,
            "san_usuario": san_usuario,
            "jugada_ia": None,
            "san_ia": None,
            "fen": board.fen(),
            "terminada": True,
            "resultado": board.result(),
            "movimientos_ia": len(
                batalla["movimientos_ia"]
            ),
            "modo_ia": modo_ia
        }

    san_ia = board.san(
        movimiento_ia
    )

    board.push(movimiento_ia)

    # Solo las jugadas provenientes del repertorio
    # cuentan dentro de las 7 jugadas imitadas.
    if modo_ia == "repertorio":

        batalla["movimientos_ia"].append(
            movimiento_ia.uci()
        )

    # -----------------------------------------------------
    # COMPROBAR FINAL
    # -----------------------------------------------------

    if board.is_game_over():
        batalla["terminada"] = True

    return {
        "ok": True,

        "san_usuario": san_usuario,

        "jugada_ia": movimiento_ia.uci(),

        "san_ia": san_ia,

        "fen": board.fen(),

        "terminada": batalla["terminada"],

        "resultado": (
            board.result()
            if batalla["terminada"]
            else None
        ),

        "movimientos_ia": len(
            batalla["movimientos_ia"]
        ),

        "modo_ia": modo_ia,

        "mensaje": (
            f"La IA está reproduciendo el repertorio "
            f"real de {batalla['usuario']}"
            if modo_ia == "repertorio"
            else "Stockfish está jugando"
        )
    }


# =========================================================
# ANALIZAR JUGADOR
# =========================================================

@app.get("/api/analizar/{usuario}")
def analizar_usuario(usuario: str):

    perfil = construir_perfil(usuario)

    return {
        "usuario": perfil["usuario"],
        "partidas": perfil["partidas"],
        "partidas_blancas": perfil["partidas_blancas"],
        "partidas_negras": perfil["partidas_negras"]
    }


# =========================================================
# PERFIL
# =========================================================

@app.get("/api/perfil/{usuario}")
def obtener_perfil(usuario: str):

    perfil = construir_perfil(usuario)

    primeras_jugadas = {
        "white": {},
        "black": {}
    }

    for color in ["white", "black"]:

        for numero in range(1, 8):

            contador = perfil[
                "movimientos_por_numero"
            ][color].get(
                numero,
                Counter()
            )

            primeras_jugadas[color][numero] = [
                {
                    "uci": uci,
                    "veces": cantidad
                }
                for uci, cantidad in contador.most_common(10)
            ]

    return {
        "usuario": perfil["usuario"],
        "partidas": perfil["partidas"],
        "partidas_blancas": perfil["partidas_blancas"],
        "partidas_negras": perfil["partidas_negras"],
        "primeras_jugadas": primeras_jugadas
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/api")
def api_status():

    return {
        "ok": True,
        "nombre": "Chess Scout API",
        "stockfish": True
    }


# =========================================================
# CERRAR STOCKFISH
# =========================================================

@app.on_event("shutdown")
def cerrar_engine():

    global engine

    if engine is not None:

        try:
            engine.quit()
        except Exception:
            pass

        engine = None
