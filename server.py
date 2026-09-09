```python
import io
import re
import random
import requests
import chess
import chess.pgn

from collections import Counter, defaultdict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# CONFIGURACIÓN
# =========================================================

MAX_PARTIDAS = 500
LICHESS = "https://lichess.org"

app = FastAPI(title="Chess Scout")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# MEMORIA DE BATALLAS
# =========================================================

batallas = {}


# =========================================================
# MODELOS
# =========================================================

class MovimientoRequest(BaseModel):
    batalla_id: str
    movimiento: str


class NuevaBatallaRequest(BaseModel):
    usuario: str
    color: str = "white"


# =========================================================
# UTILIDADES
# =========================================================

def obtener_header(partida, nombre):
    return partida.headers.get(nombre, "")


def limpiar_usuario(usuario):
    return usuario.strip()


def resultado_jugador(partida, usuario):
    white = obtener_header(partida, "White").lower()
    black = obtener_header(partida, "Black").lower()
    result = obtener_header(partida, "Result")

    usuario = usuario.lower()

    if white == usuario:
        if result == "1-0":
            return "victoria"
        if result == "0-1":
            return "derrota"
        if result == "1/2-1/2":
            return "tablas"

    if black == usuario:
        if result == "0-1":
            return "victoria"
        if result == "1-0":
            return "derrota"
        if result == "1/2-1/2":
            return "tablas"

    return "desconocido"


def calcular_score(victorias, tablas, total):
    if total == 0:
        return 0

    return round(
        ((victorias + tablas * 0.5) / total) * 100,
        1
    )


# =========================================================
# DESCARGAR PARTIDAS DE LICHESS
# =========================================================

def descargar_partidas(usuario):
    url = (
        f"{LICHESS}/api/games/user/"
        f"{requests.utils.quote(usuario)}"
    )

    params = {
        "max": MAX_PARTIDAS,
        "moves": "true",
        "tags": "true",
        "clocks": "false",
        "evals": "false",
        "opening": "true"
    }

    respuesta = requests.get(
        url,
        params=params,
        headers={
            "Accept": "application/x-chess-pgn"
        },
        timeout=30
    )

    if respuesta.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"No existe el usuario {usuario}"
        )

    if not respuesta.ok:
        raise HTTPException(
            status_code=500,
            detail="Lichess no permitió descargar las partidas."
        )

    return respuesta.text


# =========================================================
# LEER PGN
# =========================================================

def cargar_partidas(texto):
    partidas = []

    archivo = io.StringIO(texto)

    while True:
        try:
            partida = chess.pgn.read_game(archivo)
        except Exception:
            break

        if partida is None:
            break

        partidas.append(partida)

    return partidas


# =========================================================
# PERFIL DEL JUGADOR
# =========================================================

def crear_perfil(partidas, usuario):

    usuario_lower = usuario.lower()

    aperturas = Counter()

    movimientos = Counter()

    respuestas = Counter()

    posiciones = defaultdict(Counter)

    colores = Counter()

    resultados = Counter()

    primeros_movimientos = Counter()

    movimientos_por_turno = defaultdict(Counter)

    total_partidas = 0

    for partida in partidas:

        white = partida.headers.get("White", "")
        black = partida.headers.get("Black", "")

        if (
            white.lower() != usuario_lower
            and
            black.lower() != usuario_lower
        ):
            continue

        total_partidas += 1

        resultado = resultado_jugador(
            partida,
            usuario
        )

        resultados[resultado] += 1

        if white.lower() == usuario_lower:
            color = "white"
            colores["white"] += 1
        else:
            color = "black"
            colores["black"] += 1

        # ---------------------------------------------
        # APERTURA
        # ---------------------------------------------

        apertura = (
            partida.headers.get("Opening")
            or partida.headers.get("ECO")
            or "Desconocida"
        )

        aperturas[apertura] += 1

        # ---------------------------------------------
        # RECORRER MOVIMIENTOS
        # ---------------------------------------------

        board = partida.board()

        turno_jugador = 0

        for move in partida.mainline_moves():

            jugador_mueve = (
                board.turn == chess.WHITE
                if color == "white"
                else board.turn == chess.BLACK
            )

            san = board.san(move)

            if jugador_mueve:

                movimientos[san] += 1

                turno_jugador += 1

                movimientos_por_turno[
                    turno_jugador
                ][san] += 1

                # Primer movimiento del jugador
                if turno_jugador <= 3:
                    primeros_movimientos[san] += 1

                # Guardar posición antes de mover
                fen = board.fen()

                posiciones[fen][san] += 1

            board.push(move)

    # ---------------------------------------------
    # CONVERTIR A JSON
    # ---------------------------------------------

    def top(counter, cantidad=15):

        return [
            {
                "movimiento": movimiento,
                "veces": veces
            }
            for movimiento, veces
            in counter.most_common(cantidad)
        ]

    return {
        "usuario": usuario,
        "partidas": total_partidas,

        "resultados": {
            "victorias": resultados["victoria"],
            "tablas": resultados["tablas"],
            "derrotas": resultados["derrota"]
        },

        "score": calcular_score(
            resultados["victoria"],
            resultados["tablas"],
            total_partidas
        ),

        "colores": dict(colores),

        "aperturas": [
            {
                "nombre": nombre,
                "partidas": cantidad
            }
            for nombre, cantidad
            in aperturas.most_common(30)
        ],

        "movimientos_frecuentes": top(
            movimientos,
            30
        ),

        "primeros_movimientos": top(
            primeros_movimientos,
            20
        ),

        "movimientos_por_turno": {
            str(turno): top(counter, 15)
            for turno, counter
            in movimientos_por_turno.items()
        },

        # Las posiciones se mantienen internamente
        # para la IA.
        "_posiciones": posiciones
    }


# =========================================================
# PERFIL JSON SEGURO
# =========================================================

def perfil_publico(perfil):

    copia = dict(perfil)

    copia.pop(
        "_posiciones",
        None
    )

    return copia


# =========================================================
# OBTENER JUGADAS PARECIDAS AL JUGADOR
# =========================================================

def elegir_jugada_estilo(
    board,
    perfil,
    profundidad_estilo=0.80
):

    posiciones = perfil.get(
        "_posiciones",
        {}
    )

    fen = board.fen()

    # -----------------------------------------------------
    # CASO 1:
    # EL JUGADOR YA JUGÓ EN ESTA POSICIÓN
    # -----------------------------------------------------

    if fen in posiciones:

        opciones = posiciones[fen]

        movimientos = list(
            opciones.keys()
        )

        pesos = list(
            opciones.values()
        )

        # Elegir una de las jugadas que
        # realmente utilizó el jugador.

        if movimientos:

            try:

                jugadas_legales = []

                for san in movimientos:

                    try:
                        move = board.parse_san(san)

                        if move in board.legal_moves:
                            jugadas_legales.append(
                                (move, opciones[san])
                            )

                    except Exception:
                        pass

                if jugadas_legales:

                    movimientos = [
                        x[0]
                        for x in jugadas_legales
                    ]

                    pesos = [
                        x[1]
                        for x in jugadas_legales
                    ]

                    return random.choices(
                        movimientos,
                        weights=pesos,
                        k=1
                    )[0]

            except Exception:
                pass

    # -----------------------------------------------------
    # CASO 2:
    # USAR MOVIMIENTOS FRECUENTES
    # -----------------------------------------------------

    candidatos = []

    frecuentes = perfil.get(
        "movimientos_frecuentes",
        []
    )

    for dato in frecuentes:

        san = dato["movimiento"]
        frecuencia = dato["veces"]

        try:

            move = board.parse_san(san)

            if move in board.legal_moves:

                candidatos.append(
                    (
                        move,
                        frecuencia
                    )
                )

        except Exception:
            pass

    if candidatos:

        movimientos = [
            x[0]
            for x in candidatos
        ]

        pesos = [
            x[1]
            for x in candidatos
        ]

        return random.choices(
            movimientos,
            weights=pesos,
            k=1
        )[0]

    # -----------------------------------------------------
    # CASO 3:
    # SI NO CONOCEMOS LA POSICIÓN
    # ELEGIR UNA JUGADA LEGAL
    # -----------------------------------------------------

    legales = list(
        board.legal_moves
    )

    if not legales:
        return None

    return random.choice(
        legales
    )


# =========================================================
# INICIAR BATALLA
# =========================================================

@app.post("/api/batalla/iniciar")
def iniciar_batalla(datos: NuevaBatallaRequest):

    usuario = limpiar_usuario(
        datos.usuario
    )

    if not usuario:
        raise HTTPException(
            status_code=400,
            detail="Falta el usuario."
        )

    partidas_texto = descargar_partidas(
        usuario
    )

    partidas = cargar_partidas(
        partidas_texto
    )

    if not partidas:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron partidas."
        )

    perfil = crear_perfil(
        partidas,
        usuario
    )

    batalla_id = str(
        random.randint(
            100000000,
            999999999
        )
    )

    board = chess.Board()

    color_usuario = datos.color.lower()

    if color_usuario not in (
        "white",
        "black"
    ):
        color_usuario = "white"

    # -----------------------------------------------------
    # SI LA IA JUEGA CON BLANCAS
    # -----------------------------------------------------

    ia_movio = None

    if color_usuario == "black":

        ia_movio = elegir_jugada_estilo(
            board,
            perfil
        )

        if ia_movio:
            board.push(
                ia_movio
            )

    # -----------------------------------------------------
    # GUARDAR BATALLA
    # -----------------------------------------------------

    batallas[batalla_id] = {
        "usuario": usuario,
        "perfil": perfil,
        "board": board,
        "color_usuario": color_usuario
    }

    return {
        "batalla_id": batalla_id,
        "usuario": usuario,
        "color_usuario": color_usuario,
        "fen": board.fen(),
        "turno": (
            "white"
            if board.turn == chess.WHITE
            else "black"
        ),
        "ia_movimiento": (
            ia_movio.uci()
            if ia_movio
            else None
        ),
        "perfil": perfil_publico(
            perfil
        )
    }


# =========================================================
# JUGADA DEL USUARIO
# =========================================================

@app.post("/api/batalla/movimiento")
def movimiento(datos: MovimientoRequest):

    if datos.batalla_id not in batallas:

        raise HTTPException(
            status_code=404,
            detail="La batalla no existe."
        )

    batalla = batallas[
        datos.batalla_id
    ]

    board = batalla["board"]

    perfil = batalla["perfil"]

    # -----------------------------------------------------
    # CONVERTIR JUGADA
    # -----------------------------------------------------

    try:

        try:
            move = chess.Move.from_uci(
                datos.movimiento
            )

            if move not in board.legal_moves:
                raise ValueError()

        except Exception:

            move = board.parse_san(
                datos.movimiento
            )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Movimiento ilegal."
        )

    # -----------------------------------------------------
    # HACER JUGADA DEL USUARIO
    # -----------------------------------------------------

    board.push(move)

    # -----------------------------------------------------
    # COMPROBAR FINAL
    # -----------------------------------------------------

    if board.is_game_over():

        return {
            "fen": board.fen(),
            "fin": True,
            "resultado": board.result(),
            "ia_movimiento": None
        }

    # -----------------------------------------------------
    # IA IMITA AL JUGADOR
    # -----------------------------------------------------

    ia_move = elegir_jugada_estilo(
        board,
        perfil
    )

    if ia_move is None:

        return {
            "fen": board.fen(),
            "fin": True,
            "resultado": board.result(),
            "ia_movimiento": None
        }

    san_ia = board.san(
        ia_move
    )

    board.push(
        ia_move
    )

    # -----------------------------------------------------
    # COMPROBAR FINAL
    # -----------------------------------------------------

    fin = board.is_game_over()

    return {
        "fen": board.fen(),
        "fin": fin,
        "resultado": (
            board.result()
            if fin
            else None
        ),
        "ia_movimiento": ia_move.uci(),
        "ia_movimiento_san": san_ia
    }


# =========================================================
# ANALIZAR JUGADOR
# =========================================================

@app.get("/api/analizar/{usuario}")
def analizar(usuario: str):

    usuario = limpiar_usuario(
        usuario
    )

    partidas_texto = descargar_partidas(
        usuario
    )

    partidas = cargar_partidas(
        partidas_texto
    )

    if not partidas:

        raise HTTPException(
            status_code=404,
            detail="No se encontraron partidas públicas."
        )

    perfil = crear_perfil(
        partidas,
        usuario
    )

    resultados = perfil[
        "resultados"
    ]

    return {
        "usuario": usuario,
        "total": perfil["partidas"],
        "victorias": resultados["victorias"],
        "tablas": resultados["tablas"],
        "derrotas": resultados["derrotas"],
        "score": perfil["score"],
        "aperturas": perfil["aperturas"],
        "movimientos_frecuentes":
            perfil["movimientos_frecuentes"],
        "primeros_movimientos":
            perfil["primeros_movimientos"]
    }


# =========================================================
# PERFIL DEL JUGADOR
# =========================================================

@app.get("/api/perfil/{usuario}")
def perfil_usuario(usuario: str):

    usuario = limpiar_usuario(
        usuario
    )

    partidas_texto = descargar_partidas(
        usuario
    )

    partidas = cargar_partidas(
        partidas_texto
    )

    if not partidas:

        raise HTTPException(
            status_code=404,
            detail="No se encontraron partidas."
        )

    perfil = crear_perfil(
        partidas,
        usuario
    )

    return perfil_publico(
        perfil
    )


# =========================================================
# ESTADO
# =========================================================

@app.get("/")
def inicio():

    return {
        "nombre": "Chess Scout",
        "estado": "online",
        "mensaje": "Servidor funcionando correctamente."
    }


# =========================================================
# EJECUTAR
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )
```
