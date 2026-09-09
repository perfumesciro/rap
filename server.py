from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import requests
import chess.pgn
import io
from collections import defaultdict

app = FastAPI()


# --------------------------------------------------
# PÁGINA PRINCIPAL
# --------------------------------------------------

@app.get("/")
def inicio():
    return FileResponse("index.html")


# --------------------------------------------------
# BUSCAR INFORMACIÓN DEL JUGADOR EN LICHESS
# --------------------------------------------------

def obtener_jugador(usuario):

    url = f"https://lichess.org/api/user/{usuario}"

    respuesta = requests.get(
        url,
        timeout=20
    )

    if respuesta.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="No se encontró ese jugador en Lichess."
        )

    if respuesta.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail="No se pudo obtener la información del jugador."
        )

    return respuesta.json()


# --------------------------------------------------
# DESCARGAR PARTIDAS
# --------------------------------------------------

def obtener_partidas(usuario, cantidad=500):

    url = f"https://lichess.org/api/games/user/{usuario}"

    parametros = {
        "max": cantidad,
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
        params=parametros,
        headers=headers,
        timeout=60
    )

    if respuesta.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail="No se pudieron descargar las partidas."
        )

    return respuesta.text


# --------------------------------------------------
# RESULTADO DESDE EL PUNTO DE VISTA DEL JUGADOR
# --------------------------------------------------

def resultado_jugador(game, usuario):

    blancas = game.headers.get("White", "")
    negras = game.headers.get("Black", "")
    resultado = game.headers.get("Result", "*")

    usuario_lower = usuario.lower()

    if blancas.lower() == usuario_lower:

        if resultado == "1-0":
            return "victoria"

        if resultado == "0-1":
            return "derrota"

        if resultado == "1/2-1/2":
            return "tablas"

    elif negras.lower() == usuario_lower:

        if resultado == "0-1":
            return "victoria"

        if resultado == "1-0":
            return "derrota"

        if resultado == "1/2-1/2":
            return "tablas"

    return "otro"


# --------------------------------------------------
# APERTURA
# --------------------------------------------------

def obtener_apertura(game):

    apertura = game.headers.get(
        "Opening",
        ""
    )

    eco = game.headers.get(
        "ECO",
        ""
    )

    if apertura:
        return apertura

    if eco:
        return f"ECO {eco}"

    return "Apertura desconocida"


# --------------------------------------------------
# LÍNEA INICIAL DE LA PARTIDA
# --------------------------------------------------

def obtener_linea(game, cantidad_movimientos=12):

    try:

        board = game.board()

        movimientos = []

        for i, move in enumerate(
            game.mainline_moves()
        ):

            if i >= cantidad_movimientos:
                break

            movimientos.append(
                board.san(move)
            )

            board.push(move)

        linea = []

        for i in range(0, len(movimientos), 2):

            numero = i // 2 + 1

            if i + 1 < len(movimientos):

                linea.append(
                    f"{numero}. "
                    f"{movimientos[i]} "
                    f"{movimientos[i + 1]}"
                )

            else:

                linea.append(
                    f"{numero}. "
                    f"{movimientos[i]}"
                )

        return " ".join(linea)

    except Exception:

        return ""


# --------------------------------------------------
# ANALIZAR APERTURAS
# --------------------------------------------------

def analizar_aperturas(partidas, usuario):

    datos = defaultdict(
        lambda: {
            "partidas": 0,
            "victorias": 0,
            "tablas": 0,
            "derrotas": 0
        }
    )

    for game in partidas:

        resultado = resultado_jugador(
            game,
            usuario
        )

        if resultado == "otro":
            continue

        apertura = obtener_apertura(game)

        datos[apertura]["partidas"] += 1

        if resultado == "victoria":
            datos[apertura]["victorias"] += 1

        elif resultado == "tablas":
            datos[apertura]["tablas"] += 1

        elif resultado == "derrota":
            datos[apertura]["derrotas"] += 1

    resultado_final = []

    for nombre, valores in datos.items():

        total = valores["partidas"]

        puntuacion = (
            valores["victorias"]
            + valores["tablas"] * 0.5
        )

        porcentaje = round(
            puntuacion / total * 100,
            1
        )

        resultado_final.append({

            "nombre": nombre,

            "partidas": total,

            "victorias":
                valores["victorias"],

            "tablas":
                valores["tablas"],

            "derrotas":
                valores["derrotas"],

            "porcentaje":
                porcentaje

        })

    resultado_final.sort(
        key=lambda x: x["partidas"],
        reverse=True
    )

    return resultado_final


# --------------------------------------------------
# BUSCAR VARIANTES PROBLEMÁTICAS
# --------------------------------------------------

def analizar_variantes(partidas, usuario):

    variantes = defaultdict(
        lambda: {
            "partidas": 0,
            "victorias": 0,
            "tablas": 0,
            "derrotas": 0
        }
    )

    for game in partidas:

        resultado = resultado_jugador(
            game,
            usuario
        )

        if resultado == "otro":
            continue

        apertura = obtener_apertura(game)

        linea = obtener_linea(
            game,
            12
        )

        if not linea:
            continue

        clave = (
            apertura,
            linea
        )

        variantes[clave]["partidas"] += 1

        if resultado == "victoria":
            variantes[clave]["victorias"] += 1

        elif resultado == "tablas":
            variantes[clave]["tablas"] += 1

        elif resultado == "derrota":
            variantes[clave]["derrotas"] += 1

    resultado_final = []

    for (apertura, linea), valores in variantes.items():

        total = valores["partidas"]

        if total < 2:
            continue

        puntuacion = (
            valores["victorias"]
            + valores["tablas"] * 0.5
        )

        porcentaje = round(
            puntuacion / total * 100,
            1
        )

        resultado_final.append({

            "apertura": apertura,

            "linea": linea,

            "partidas": total,

            "victorias":
                valores["victorias"],

            "tablas":
                valores["tablas"],

            "derrotas":
                valores["derrotas"],

            "porcentaje":
                porcentaje
        })

    # Las variantes con peor puntuación primero
    resultado_final.sort(
        key=lambda x: (
            x["porcentaje"],
            -x["partidas"]
        )
    )

    return resultado_final[:15]


# --------------------------------------------------
# CONVERTIR PARTIDAS PARA EL FRONTEND
# --------------------------------------------------

def preparar_partidas(partidas, usuario):

    resultado_final = []

    for game in partidas:

        resultado = resultado_jugador(
            game,
            usuario
        )

        if resultado == "otro":
            continue

        resultado_pgn = game.headers.get(
            "Result",
            "*"
        )

        fecha = game.headers.get(
            "Date",
            "Fecha desconocida"
        )

        resultado_final.append({

            "blancas":
                game.headers.get(
                    "White",
                    "?"
                ),

            "negras":
                game.headers.get(
                    "Black",
                    "?"
                ),

            "fecha":
                fecha,

            "apertura":
                obtener_apertura(game),

            "resultado":
                resultado,

            "resultado_pgn":
                resultado_pgn
        })

    return resultado_final


# --------------------------------------------------
# ANALIZAR JUGADOR
# --------------------------------------------------

@app.get("/api/analizar/{usuario}")
def analizar(usuario: str):

    jugador = obtener_jugador(
        usuario
    )

    pgn = obtener_partidas(
        usuario,
        500
    )

    archivo = io.StringIO(pgn)

    partidas = []

    while True:

        try:

            game = chess.pgn.read_game(
                archivo
            )

        except Exception:

            break

        if game is None:
            break

        partidas.append(game)

    if not partidas:

        raise HTTPException(
            status_code=404,
            detail="No se encontraron partidas públicas."
        )

    victorias = 0
    tablas = 0
    derrotas = 0

    for game in partidas:

        resultado = resultado_jugador(
            game,
            usuario
        )

        if resultado == "victoria":
            victorias += 1

        elif resultado == "tablas":
            tablas += 1

        elif resultado == "derrota":
            derrotas += 1

    total = (
        victorias
        + tablas
        + derrotas
    )

    puntuacion = 0

    if total > 0:

        puntuacion = round(
            (
                victorias
                + tablas * 0.5
            )
            / total
            * 100,
            1
        )

    aperturas = analizar_aperturas(
        partidas,
        usuario
    )

    variantes = analizar_variantes(
        partidas,
        usuario
    )

    lista_partidas = preparar_partidas(
        partidas,
        usuario
    )

    return {

        "jugador": {

            "nombre":
                jugador.get(
                    "username",
                    usuario
                ),

            "id":
                jugador.get(
                    "username",
                    usuario
                ),

            "titulo":
                jugador.get(
                    "title",
                    ""
                )
        },

        "estadisticas": {

            "total":
                total,

            "victorias":
                victorias,

            "tablas":
                tablas,

            "derrotas":
                derrotas,

            "porcentaje":
                puntuacion
        },

        "aperturas":
            aperturas,

        "variantes_problematicas":
            variantes,

        "partidas":
            lista_partidas
    }
