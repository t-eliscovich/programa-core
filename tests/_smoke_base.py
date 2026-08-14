"""Base compartida del POST smoke de facturas / cheques / bancos.

POR QUÉ EXISTE
--------------
`test_routes_smoke.py` recorre las rutas GET con la base vacía y un stub de
`db`. Eso prueba que la pantalla ABRE; no prueba que el botón HAGA algo. El
14/08/2026 medimos que de las 60 rutas que aceptan POST en estos tres módulos
los tests posteaban a 11, y los tres archivos quedaban en 20-24% de cobertura.
El 500 de conciliación del 13/08 —una clave que faltaba en el dict que iba al
template— era exactamente de esa clase: se veía sólo posteando.

Este módulo trae lo COMÚN: levantar la app contra un Postgres real, loguear un
usuario con todos los permisos, postear y clasificar la respuesta. Lo que cada
módulo SIEMBRA es propio suyo y vive en su `test_post_smoke_<mod>.py`.

POR QUÉ POSTGRES REAL Y NO EL FakeDB
------------------------------------
Con el FakeDB del conftest todos los POST a `/<algo>/<int:id>/…` mueren en "no
existe": cubrirían las tres líneas del lookup y nada más, y el barrido sería
falso. Por eso estos tests van marcados `@pytest.mark.db`.
"""
from __future__ import annotations

import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg2
import psycopg2.extras

#: Códigos que NO son un bug. Un POST con payload incompleto tiene que rebotar
#: de forma controlada: 400/404/409 explícito, o redirect al formulario con un
#: flash. Lo único que este smoke persigue es el 500 y el traceback.
OK_ESPERADOS = frozenset({200, 201, 204, 302, 303, 400, 403, 404, 409, 422})

#: Los mismos que vigila `_no_dejar_db_pisado` en el conftest.
_DB_VIGILADOS = ("fetch_one", "fetch_all", "execute", "execute_returning",
                 "tx", "get_conn", "init_pool", "close_pool")


@dataclass
class Resultado:
    """Una ruta posteada y lo que contestó."""

    url: str
    metodo: str
    status: int
    error: str | None = None
    nota: str = ""

    @property
    def es_500(self) -> bool:
        return self.status >= 500 or self.error is not None

    def __str__(self) -> str:  # pragma: no cover - sólo para el mensaje de falla
        base = f"{self.metodo} {self.url} -> {self.status}"
        if self.nota:
            base += f"  ({self.nota})"
        if self.error:
            base += "\n" + self.error
        return base


@dataclass
class Barrido:
    """Junta los resultados de un módulo para reportarlos TODOS de una.

    Que falle de a una obliga a arreglar-correr-arreglar; el barrido entero en
    un mensaje deja ver si lo que se rompió es una ruta o un patrón.
    """

    modulo: str
    resultados: list = field(default_factory=list)

    def agregar(self, r: Resultado) -> Resultado:
        self.resultados.append(r)
        return r

    @property
    def rotos(self) -> list:
        return [r for r in self.resultados if r.es_500]

    def reporte(self) -> str:
        lineas = [
            f"{len(self.rotos)} de {len(self.resultados)} rutas de {self.modulo} "
            f"contestaron 500 o levantaron excepción:",
            "",
        ]
        lineas += [f"  ✗ {r}" for r in self.rotos]
        return "\n".join(lineas)


def _conectar(dbname: str | None = None):
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        user=os.environ["DB_USER"],
        dbname=dbname or os.environ["DB_NAME"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def conexion(dbname: str | None = None):
    """Conexión directa al Postgres de la sesión, con la base ya migrada.

    Los tests SIEMBRAN por SQL —no por la UI— a propósito: acá no estamos
    operando producción, estamos construyendo el escenario mínimo para poder
    apretar el botón. El botón sí se aprieta por la ruta real.

    Migrar cuelga de acá, y no de cada fixture, porque es la única puerta por
    la que pasan los tres barridos: si mañana alguien agrega un cuarto módulo,
    no puede olvidárselo.
    """
    nombre = dbname or os.environ["DB_NAME"]
    asegurar_migrada(nombre)
    return _conectar(nombre)


_MIGRADAS: set = set()


def asegurar_migrada(dbname: str) -> None:
    """Aplica las migraciones a `dbname` si le faltan, una sola vez por sesión.

    El CI restaura `legacy_minimal_dump.sql` y corre pytest — pero el dump es
    la foto del dBase, SIN las migraciones encima. `scintela.mov_doble` (y todo
    lo que Programa Core agregó después) no existe ahí, así que un barrido que
    siembra contra esa base muere en la siembra con `UndefinedTable`. Medido el
    14/08 reproduciendo el camino exacto del CI.

    El runner lleva su propia cuenta de lo aplicado, así que llamarlo de más no
    hace nada: con la base al día sale en cero. Es el mismo camino que usa el
    fixture `migrated_db` del `conftest_db`, sin pedirle el DSN en forma de URL
    —que no se puede armar cuando `DB_HOST` es un directorio de socket—.
    """
    if dbname in _MIGRADAS:
        return
    con = _conectar(dbname)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT to_regclass('scintela.mov_doble') AS t")
            if cur.fetchone()["t"] is not None:
                _MIGRADAS.add(dbname)
                return
    finally:
        con.close()

    import importlib

    previo = os.environ.get("DB_NAME")
    os.environ["DB_NAME"] = dbname
    try:
        import db as real_db

        importlib.reload(real_db)
        from scripts import migrate

        importlib.reload(migrate)
        migrate.main([])
    finally:
        if previo is not None:
            os.environ["DB_NAME"] = previo
    _MIGRADAS.add(dbname)


def db_smoke() -> str:
    """En qué base corre el barrido.

    En el CI hay UNA sola base (`programa_core_test`, la que restaura
    `restaurar_test_legacy_dump.py`) y `DB_NAME` la nombra: los tres barridos
    tienen que caer ahí, o se skipean solos y quedan dormidos — que es la forma
    más silenciosa de perder un test.

    Local, para correr los tres en paralelo sin pisarse, cada uno puede apuntar
    a la suya con `PC_SMOKE_DB`. Igual están escritos para convivir en una
    misma base: siembran con códigos propios y ninguno borra tablas enteras.
    """
    return os.environ.get("PC_SMOKE_DB") or os.environ.get("DB_NAME") or "programa_core"


@contextmanager
def app_smoke(dbname: str):
    """La app real apuntando a `dbname`, con un usuario logueado y `*`.

    Se pisa `g.user` en un `before_request` que corre DESPUÉS de
    `load_logged_in_user`, igual que hace `scripts/vista_local.py`: es la forma
    que ya usa la casa para saltear el login sin tocar `auth`.

    Los permisos van todos (`*`) porque lo que se persigue es el 500, no el
    403. Que una ruta conteste 403 CON permisos completos sí es un hallazgo:
    queda anotado en el resultado, no rompe el test.
    """
    import importlib

    os.environ["DB_NAME"] = dbname
    import db as real_db

    # `reload` deja el módulo `db` apuntando al Postgres real. Si no se
    # restaura, el test siguiente del mismo worker cree tener el FakeDB y no lo
    # tiene: el detector `_no_dejar_db_pisado` del conftest lo caza y avisa
    # FUGA. La idea es que esa lista llegue a cero para poder poner el detector
    # en estricto, así que restauramos nosotros en vez de dejar el aviso.
    _previos = {n: getattr(real_db, n, None) for n in _DB_VIGILADOS}

    importlib.reload(real_db)

    from flask import g

    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_smoke():
        g.user = {
            "id_usuario": 0,
            "username": "smoke",
            "id_rol": 0,
            "nombre_rol": "Accionista",
            "activo": True,
            "vend": None,
        }
        g.permisos = {"*"}

    try:
        yield app
    finally:
        try:
            real_db.close_pool()
        except Exception:  # pragma: no cover - el pool puede no haber abierto
            pass
        for _n, _v in _previos.items():
            if _v is not None:
                setattr(real_db, _n, _v)


def postear(client, url: str, data: dict | None = None, *, json=None, nota: str = "") -> Resultado:
    """Postea y devuelve el Resultado sin dejar que la excepción corte el barrido.

    Con TESTING=True el test_client propaga la excepción de la vista. La
    atrapamos para poder seguir con las otras rutas y reportarlas todas juntas.
    """
    try:
        if json is not None:
            resp = client.post(url, json=json)
        else:
            resp = client.post(url, data=data or {})
    except Exception:
        return Resultado(url, "POST", 599, traceback.format_exc(limit=12), nota)
    return Resultado(url, "POST", resp.status_code, None, nota)


def getear(client, url: str, *, nota: str = "") -> Resultado:
    """Igual que `postear` pero GET — para las rutas con parámetro en la URL,
    que el smoke viejo saltea justamente porque necesitan un id real."""
    try:
        resp = client.get(url)
    except Exception:
        return Resultado(url, "GET", 599, traceback.format_exc(limit=12), nota)
    return Resultado(url, "GET", resp.status_code, None, nota)


# ---------------------------------------------------------------------------
# El 302 que esconde el crash
# ---------------------------------------------------------------------------
# TMT 2026-08-14 — los tres módulos terminan sus vistas igual:
#
#     except ValueError as e:  flash(str(e), "warn")       ← rechazo del negocio
#     except Exception as e:   flash_exc("No pude …", e)   ← CRASH
#     return redirect(...)
#
# Las DOS salidas contestan 302. Un smoke que mira sólo el status code da verde
# con la vista reventando adentro — o sea, verde por la razón equivocada, que es
# peor que rojo. Dos de los tres barridos del 14/08 llegaron a esto por
# separado; por eso vive acá y no copiado en cada archivo.
#
# Espiar `flash_exc` es lo que convierte este smoke en algo que sostiene una
# afirmación: no "ninguna ruta contestó 500" sino "ninguna ruta tragó una
# excepción". Se verifica inyectando un `raise` en una query y mirando que la
# ruta conteste 302 y el barrido se ponga rojo igual.


class CazaTragadas:
    """Registra TODA excepción que la vista atrapó y decidió no propagar.

    `modulo` es el módulo de vistas a espiar (`modules.bancos.views`, etc.).

    Espiar sólo `flash_exc` NO alcanza: en los tres módulos hay 137 `except
    Exception` y sólo 53 terminan en `flash_exc`. Los otros hacen
    `errores.append(f"Error al editar: {humanize(e)}")` y devuelven un
    render con **400**, o `jsonify(ok=False, error=humanize(e)), 500`. El 400
    está en `OK_ESPERADOS` —tiene que estarlo, es la respuesta correcta a un
    formulario incompleto—, así que una vista que revienta ahí adentro pasaba
    el barrido sin que se notara.

    Lo que tienen TODOS en común es `humanize(exc)`: es el único camino por el
    que una excepción se convierte en texto para el usuario, y `flash_exc` lo
    llama por dentro. Por eso el espía va sobre `humanize` —en el módulo donde
    está definido y en el que lo importó por nombre— y no sobre `flash_exc`.
    """

    def __init__(self, modulo):
        self.modulo = modulo
        self.pendientes: list = []
        self._originales: list = []

    def _espiar(self, obj, nombre):
        original = getattr(obj, nombre, None)
        if original is None:
            return

        def _envuelto(exc, *a, **kw):
            self.pendientes.append(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )
            return original(exc, *a, **kw)

        setattr(obj, nombre, _envuelto)
        self._originales.append((obj, nombre, original))

    def __enter__(self):
        import error_messages

        # En error_messages, porque es por donde pasa `flash_exc`; y en el
        # módulo de vistas, porque lo importa por nombre (`from
        # error_messages import humanize`) y esa referencia es otra.
        self._espiar(error_messages, "humanize")
        self._espiar(self.modulo, "humanize")
        return self

    def __exit__(self, *_exc):
        for obj, nombre, original in self._originales:
            setattr(obj, nombre, original)
        self._originales.clear()
        return False

    def tomar(self) -> str | None:
        """Devuelve y limpia lo acumulado. Se llama ANTES y DESPUÉS de cada
        request: antes para no atribuirle a esta ruta lo que tragó la anterior."""
        if not self.pendientes:
            return None
        txt = "\n".join(self.pendientes)
        self.pendientes.clear()
        return "EXCEPCIÓN TRAGADA POR flash_exc:\n" + txt


def post_cazando(client, barrido, caza, url, data=None, *, json=None, nota=""):
    """`postear` + la caza: un 302 con excepción tragada queda ROTO."""
    caza.tomar()  # descarta lo que haya quedado de la ruta anterior
    r = postear(client, url, data=data, json=json, nota=nota)
    r.error = caza.tomar() or r.error
    return barrido.agregar(r)


def get_cazando(client, barrido, caza, url, *, nota=""):
    caza.tomar()
    r = getear(client, url, nota=nota)
    r.error = caza.tomar() or r.error
    return barrido.agregar(r)


def postear_cazando(client, caza, url, data=None, *, json=None, nota=""):
    """`postear` + la caza, para envolver en `barrido.agregar(...)`.

    Misma firma que `postear` con la caza intercalada: se puede sustituir en
    una línea sin tocar el resto de la llamada.
    """
    caza.tomar()  # descarta lo que haya quedado de la ruta anterior
    r = postear(client, url, data=data, json=json, nota=nota)
    r.error = caza.tomar() or r.error
    return r


def getear_cazando(client, caza, url, *, nota=""):
    caza.tomar()
    r = getear(client, url, nota=nota)
    r.error = caza.tomar() or r.error
    return r
