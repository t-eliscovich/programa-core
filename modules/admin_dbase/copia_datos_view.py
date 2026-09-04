"""/admin/copia-de-datos — una copia completa de la base, para llevar.

TMT 2026-09-03 (dueña: *"can you save the data here as well?"*). Los
respaldos de RDS (35 días, automáticos) viven ADENTRO de la cuenta de AWS:
si un día la cuenta o la instancia desaparecen, se van con ella. Esta
pantalla saca la base afuera: un zip con un CSV por tabla de `scintela` y
`seguridad`, la lista de migraciones aplicadas y un LEEME con cómo volver a
cargarla. RDS no se alcanza desde afuera y el EC2 no tiene `pg_dump`, así
que la copia la arma el programa mismo con `COPY ... TO STDOUT` (el mismo
camino que ya usaba `scripts/sync_dbase_actual.py` como respaldo pre-sync).

Dos formas de bajarla:
  - a mano: el botón de la pantalla (permiso `usuarios.admin`);
  - sola, todos los lunes: `GET /admin/copia-de-datos/descargar?clave=...`
    con la CLAVE DE DESCARGA, que vive en `.clave_copia` al lado del
    programa (nace sola, como `.secret_key`, y NO viaja en el deploy). La
    pantalla la muestra para copiarla a la máquina que baja la copia.

La copia es SOLO LECTURA: una transacción READ ONLY, igual que la consola
SQL. Pesa ~160 MB adentro de Postgres; comprimida, unas decenas de MB.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import tempfile
import zipfile

from flask import Blueprint, abort, render_template, request, send_file

import db
from auth import requiere_login, requiere_permiso
from modules.informes.foto import ahora_ec

_LOG = logging.getLogger("programa_core.admin_dbase.copia_datos")

bp = Blueprint(
    "admin_copia_datos",
    __name__,
    url_prefix="/admin/copia-de-datos",
    template_folder="templates",
)

ESQUEMAS = ("scintela", "seguridad")

_LEEME = """Copia completa de la base de Programa Core ({fecha}).

Un CSV por tabla (con encabezado), esquemas scintela y seguridad.
migraciones.txt = las migraciones que estaban aplicadas al sacar la copia.

Para volver a cargarla en un Postgres vacío:
  1. Clonar el programa y correr `python scripts/migrate.py` hasta la última
     migración de migraciones.txt (crea todas las tablas).
  2. Por cada CSV:  \\copy esquema.tabla FROM 'esquema.tabla.csv' CSV HEADER
     (antes: SET session_replication_role = replica; para que las FK no
     frenen el orden de carga).
"""


def _archivo_clave() -> str:
    return os.environ.get("CLAVE_COPIA_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".clave_copia",
    )


def clave_de_descarga() -> str:
    """La clave persistida en `.clave_copia`; si no existe, nace y se guarda.

    Mismo patrón que `_load_secret_key` en app.py: el archivo NO está en el
    repo ni en el tarball del deploy, así que sobrevive a los deploys y no
    rota sola. Si no se puede escribir, la clave vale para este proceso nada más.
    """
    ruta = _archivo_clave()
    try:
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                v = f.read().strip()
            if len(v) >= 32:
                return v
    except Exception as e:  # noqa: BLE001
        _LOG.warning("No pude leer la clave de descarga (%s): %s", ruta, e)
    nueva = secrets.token_urlsafe(48)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(nueva)
    except Exception as e:  # noqa: BLE001
        _LOG.error("No pude guardar la clave de descarga en %s: %s", ruta, e)
    return nueva


def tablas_a_copiar(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname = ANY(%s) ORDER BY 1, 2",
            (list(ESQUEMAS),),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def armar_copia(destino) -> dict:
    """Escribe el zip en `destino` (archivo abierto en binario). Devuelve
    {"tablas": n, "migracion": última versión aplicada}."""
    fecha = ahora_ec().strftime("%Y-%m-%d %H:%M")
    n = 0
    ultima = ""
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
                for esquema, tabla in tablas_a_copiar(conn):
                    buf = io.BytesIO()
                    with conn.cursor() as cur:
                        cur.copy_expert(
                            f'COPY "{esquema}"."{tabla}" TO STDOUT WITH CSV HEADER',
                            buf,
                        )
                    z.writestr(f"{esquema}.{tabla}.csv", buf.getvalue())
                    n += 1
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT version FROM seguridad.migraciones_aplicadas ORDER BY 1"
                    )
                    versiones = [str(r[0]) for r in cur.fetchall()]
                ultima = versiones[-1] if versiones else ""
                z.writestr("migraciones.txt", "\n".join(versiones) + "\n")
                z.writestr("LEEME.txt", _LEEME.format(fecha=fecha))
        finally:
            conn.rollback()
    return {"tablas": n, "migracion": ultima}


def _mandar_zip():
    tmp = tempfile.TemporaryFile()
    info = armar_copia(tmp)
    tmp.seek(0, os.SEEK_END)
    peso = tmp.tell()
    tmp.seek(0)
    nombre = f"programa-core-datos-{ahora_ec():%Y-%m-%d}.zip"
    _LOG.info("copia de datos: %s tablas, %.1f MB, hasta %s",
              info["tablas"], peso / 1e6, info["migracion"])
    return send_file(tmp, mimetype="application/zip",
                     as_attachment=True, download_name=nombre)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index():
    with db.get_conn() as conn:
        tablas = tablas_a_copiar(conn)
        conn.rollback()
    return render_template(
        "admin_dbase/copia_datos.html",
        n_tablas=len(tablas),
        clave=clave_de_descarga(),
        url_descarga=request.url_root.rstrip("/") + "/admin/copia-de-datos/descargar",
    )


@bp.route("/bajar", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def bajar():
    """El botón de la pantalla (con sesión)."""
    return _mandar_zip()


@bp.route("/descargar", methods=["GET"])
def descargar():
    """La bajada automática de los lunes: sin sesión, con la clave."""
    clave = request.args.get("clave") or request.headers.get("X-Clave-Copia") or ""
    if not clave or not secrets.compare_digest(clave, clave_de_descarga()):
        abort(403)
    return _mandar_zip()
