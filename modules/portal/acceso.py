"""Quién entra al portal del cliente, y con qué.

TMT 2026-08-24, `PLAN_PORTAL_CLIENTE_2026_08_24.md`. Un solo camino, sin ramas:

    código de 3 letras + RUC  →  elige su clave  →  entra
    después:  código + su clave
    si la olvida:  6 dígitos por mail

⚠ **El riesgo que se acepta a propósito.** El código de 3 letras y el RUC son
los dos públicos —el RUC está impreso en cada factura y se consulta gratis en
el SRI—, así que alguien con una factura vieja puede entrar antes que el
cliente. Lo que lo frena no es la puerta: es que **el vendedor lo ve enseguida
en Mi Cartera y le corta el acceso**. Se eligió así porque cualquier chequeo
previo le carga fricción al 100% de los clientes para atajar un caso raro.

Por eso todo lo de acá deja rastro: `portal_ingreso` guarda cada intento, con
qué entró y desde dónde.

Este módulo no sabe de Flask ni de pantallas: recibe datos y devuelve un
resultado. Lo que decide qué mostrar es `views.py`.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime

import bcrypt

import db

_LOG = logging.getLogger("programa_core.portal.acceso")

#: Cuántos intentos fallidos seguidos antes de trabar el acceso.
TOPE_INTENTOS = 5
#: Cuánto queda trabado. No es para siempre: el cliente se equivoca de buena fe
#: mucho más seguido de lo que alguien ataca.
MINUTOS_TRABADO = 15
#: Cuánto vive el código de 6 dígitos que llega por mail.
MINUTOS_CODIGO = 15


def _ahora() -> datetime:
    return datetime.now(UTC)


def normalizar_codigo(codigo: str) -> str:
    """El código de cliente, como lo JOINea todo el sistema: mayúsculas y sin
    espacios. Ver la mig 0155 y el skill de códigos duplicados."""
    return (codigo or "").strip().upper()


def ruc10(ruc: str) -> str:
    """Los 10 primeros dígitos del RUC, que es la llave PC↔Asinfo.

    En Ecuador el RUC de persona natural es la cédula (10 dígitos) + '001', y
    en la base a veces está la cédula pelada y a veces el RUC entero. Comparar
    los 10 primeros hace que las dos formas coincidan.
    """
    solo_numeros = re.sub(r"\D", "", ruc or "")
    return solo_numeros[:10]


# ---------------------------------------------------------------------------
# La clave
# ---------------------------------------------------------------------------


def cifrar(texto: str) -> str:
    return bcrypt.hashpw(texto.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def coincide(texto: str, cifrado: str | None) -> bool:
    """¿Este texto es el que está guardado? Nunca levanta."""
    if not texto or not cifrado:
        return False
    try:
        guardado = cifrado.encode("utf-8") if isinstance(cifrado, str) else cifrado
        return bcrypt.checkpw(texto.encode("utf-8"), guardado)
    except Exception:  # noqa: BLE001 -- un hash corrupto es "no coincide"
        return False


def clave_aceptable(clave: str) -> tuple[bool, str]:
    """¿Sirve como clave? El mensaje sale tal cual en la pantalla.

    Ocho caracteres y nada más. Pedir mayúsculas, números y un símbolo no hace
    la clave más segura de verdad y sí hace que la gente la anote en un papel.
    """
    clave = clave or ""
    if len(clave) < 8:
        return False, "La clave tiene que tener al menos 8 letras o números."
    if len(clave) > 200:
        return False, "La clave es demasiado larga."
    return True, ""


def codigo_de_seis() -> str:
    """Seis dígitos al azar, del generador seguro. Nunca `random`."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# Las filas
# ---------------------------------------------------------------------------


def cliente(codigo: str) -> dict | None:
    """La ficha del cliente, o None. Sólo lo que el portal necesita."""
    cod = normalizar_codigo(codigo)
    if not cod:
        return None
    fila = db.fetch_one(
        """
        SELECT id_cliente,
               UPPER(TRIM(codigo_cli))            AS codigo_cli,
               COALESCE(NULLIF(TRIM(nombre), ''), '') AS nombre,
               COALESCE(TRIM(ruc), '')            AS ruc,
               COALESCE(TRIM(vend), '')           AS vend
          FROM scintela.cliente
         WHERE UPPER(TRIM(codigo_cli)) = %s
        """,
        (cod,),
    )
    return dict(fila) if fila else None


def acceso(codigo: str) -> dict | None:
    cod = normalizar_codigo(codigo)
    if not cod:
        return None
    fila = db.fetch_one(
        "SELECT * FROM scintela.portal_acceso "
        " WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))
    return dict(fila) if fila else None


def anotar(codigo: str, resultado: str, con_que: str = "",
           ip: str = "", navegador: str = "") -> None:
    """Deja el intento en la bitácora. Nunca rompe el ingreso."""
    try:
        db.execute(
            "INSERT INTO scintela.portal_ingreso "
            "       (codigo_cli, resultado, con_que, ip, navegador) "
            "VALUES (%s, %s, %s, %s, %s)",
            (normalizar_codigo(codigo), resultado[:40], (con_que or "")[:20],
             (ip or "")[:60], (navegador or "")[:200]),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("portal: no pude anotar el ingreso (%s)", e)


def _sumar_fallido(cod: str) -> None:
    db.execute(
        """
        UPDATE scintela.portal_acceso
           SET intentos_fallidos = intentos_fallidos + 1,
               bloqueado_hasta = CASE WHEN intentos_fallidos + 1 >= %s
                                      THEN now() + (%s || ' minutes')::interval
                                      ELSE bloqueado_hasta END
         WHERE UPPER(TRIM(codigo_cli)) = %s
        """,
        (TOPE_INTENTOS, str(MINUTOS_TRABADO), cod),
    )


def _limpiar_fallidos(cod: str) -> None:
    db.execute(
        "UPDATE scintela.portal_acceso "
        "   SET intentos_fallidos = 0, bloqueado_hasta = NULL, "
        "       ultimo_ingreso_en = now(), "
        "       primer_ingreso_en = COALESCE(primer_ingreso_en, now()) "
        " WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))


def _crear_acceso(cod: str) -> None:
    db.execute(
        "INSERT INTO scintela.portal_acceso (codigo_cli) VALUES (%s) "
        "ON CONFLICT DO NOTHING", (cod,))


# ---------------------------------------------------------------------------
# Entrar
# ---------------------------------------------------------------------------

#: Lo que se le dice al que no entró. **Siempre lo mismo**, gane quien gane: si
#: dijera "ese código no existe" contra "la clave está mal", cualquiera podría
#: averiguar qué códigos de cliente son reales probando de a uno.
NO_ENTRO = "El código o la clave no son correctos."
TRABADO = ("Probaste demasiadas veces. Esperá {minutos} minutos, "
           "o llamanos y te destrabamos.")
CORTADO = "Tu acceso está cerrado. Llamanos y lo abrimos de nuevo."


def entrar(codigo: str, secreto: str, ip: str = "",
           navegador: str = "") -> dict:
    """Intenta entrar. Devuelve qué pasó y qué mostrar.

    ``{"ok": bool, "mensaje": str, "codigo_cli": str, "elegir_clave": bool}``

    `elegir_clave` es True cuando entró con el RUC porque todavía no tiene
    clave: la pantalla siguiente es la de elegirla.
    """
    fuera = {"ok": False, "mensaje": NO_ENTRO, "codigo_cli": "",
             "elegir_clave": False}
    cod = normalizar_codigo(codigo)
    secreto = (secreto or "").strip()
    if not cod or not secreto:
        return fuera

    fic = cliente(cod)
    if not fic:
        # Se anota igual: un montón de intentos contra códigos que no existen
        # es alguien probando de a uno, y eso se tiene que poder ver.
        anotar(cod, "no_existe", "", ip, navegador)
        return fuera

    acc = acceso(cod)
    if acc is None:
        _crear_acceso(cod)
        acc = acceso(cod) or {}

    if not acc.get("activo", True):
        anotar(cod, "cortado", "", ip, navegador)
        return {**fuera, "mensaje": CORTADO}

    trabado = acc.get("bloqueado_hasta")
    if trabado and trabado > _ahora():
        faltan = max(1, int((trabado - _ahora()).total_seconds() // 60) + 1)
        anotar(cod, "trabado", "", ip, navegador)
        return {**fuera, "mensaje": TRABADO.format(minutos=faltan)}

    # Con clave si ya la eligió; con el RUC si es la primera vez.
    tiene_clave = bool(acc.get("clave_hash"))
    if tiene_clave:
        bien = coincide(secreto, acc.get("clave_hash"))
        con_que = "clave"
    else:
        # El RUC se compara por los 10 primeros dígitos: la ficha a veces tiene
        # la cédula pelada y el cliente escribe el RUC entero, o al revés.
        propio = ruc10(fic.get("ruc"))
        bien = bool(propio) and ruc10(secreto) == propio
        con_que = "ruc"

    if not bien:
        _sumar_fallido(cod)
        anotar(cod, "clave_mala" if tiene_clave else "ruc_malo",
               con_que, ip, navegador)
        return fuera

    _limpiar_fallidos(cod)
    anotar(cod, "ok", con_que, ip, navegador)
    return {"ok": True, "mensaje": "", "codigo_cli": cod,
            "elegir_clave": not tiene_clave}


def guardar_clave(codigo: str, clave: str) -> tuple[bool, str]:
    """La clave que eligió el cliente. Desde acá el RUC ya no abre nada."""
    ok, msg = clave_aceptable(clave)
    if not ok:
        return False, msg
    cod = normalizar_codigo(codigo)
    db.execute(
        "UPDATE scintela.portal_acceso SET clave_hash = %s, "
        "       intentos_fallidos = 0, bloqueado_hasta = NULL "
        " WHERE UPPER(TRIM(codigo_cli)) = %s", (cifrar(clave), cod))
    return True, "Listo. La próxima vez entrá con tu clave."


def mail_aceptable(mail: str, repetido: str) -> tuple[bool, str]:
    """¿Sirve como correo, y lo escribió dos veces igual?

    ⭐ Se pide DOS veces a propósito: es por donde le llega la clave si la
    olvida. Una letra mal tipeada acá lo deja afuera para siempre y sin forma
    de darse cuenta — el mail sale, no rebota a la vista de nadie, y el cliente
    espera un código que nunca va a llegar.

    Vacío es válido: cargar el correo no es obligatorio. Pero si escribió uno,
    los dos tienen que coincidir.
    """
    mail = (mail or "").strip().lower()
    repetido = (repetido or "").strip().lower()
    if not mail and not repetido:
        return True, ""
    if mail != repetido:
        return False, "Los dos correos no son iguales."
    # A propósito NO se valida más que esto. Una expresión regular estricta
    # rechaza correos raros pero válidos, y el que se equivoca de dominio pasa
    # igual: para eso está el pedirlo dos veces.
    if "@" not in mail or " " in mail or len(mail) > 200:
        return False, "Ese correo no parece un correo."
    return True, ""


def guardar_mail(codigo: str, mail: str, mail_previo: str = "") -> None:
    """El mail que el cliente confirmó o corrigió.

    ⚠ NO pisa la ficha del cliente: queda acá. Pasarlo al maestro lo hace el
    vendedor o la oficina desde la pantalla de siempre. De `mail_cambiado` sale
    solo cuántos clientes lo cambiaron, que es lo que se quería medir.
    """
    mail = (mail or "").strip().lower()
    if not mail:
        return
    cambiado = mail != (mail_previo or "").strip().lower()
    db.execute(
        "UPDATE scintela.portal_acceso SET mail = %s, mail_cambiado = %s "
        " WHERE UPPER(TRIM(codigo_cli)) = %s",
        (mail[:200], cambiado, normalizar_codigo(codigo)))


# ---------------------------------------------------------------------------
# Olvidé mi clave
# ---------------------------------------------------------------------------

#: Lo que se le contesta a TODO el mundo cuando pide un código, exista o no el
#: cliente y tenga o no correo cargado. Si dijera "ese cliente no tiene correo"
#: o "ese código no existe", la pantalla de recuperación sería un buscador de
#: códigos de cliente reales.
MANDADO = ("Si ese código tiene un correo cargado, le mandamos un código de 6 "
           "números. Fijate en tu correo — puede tardar un minuto.")


def pedir_codigo(codigo: str) -> tuple[str, str]:
    """Arma el código de 6 dígitos y devuelve `(codigo_en_claro, mail)`.

    Devuelve `("", "")` cuando no hay a quién mandárselo. **El que llama
    contesta lo mismo igual**: ver `MANDADO`.

    El código va cifrado a la base, como una clave: el que pueda leer la tabla
    no tiene que poder entrar a la cuenta de nadie.
    """
    cod = normalizar_codigo(codigo)
    if not cod:
        return "", ""
    acc = acceso(cod)
    if not acc or not acc.get("activo", True):
        return "", ""
    mail = (acc.get("mail") or "").strip()
    if not mail:
        # Todavía no cargó ninguno en el portal: se cae al que ya teníamos de
        # Asinfo, que es el que el 95% de la plata tiene.
        mail = _mail_de_asinfo(cod)
    if not mail:
        return "", ""

    seis = codigo_de_seis()
    db.execute(
        "INSERT INTO scintela.portal_codigo "
        "       (codigo_cli, codigo_hash, mandado_a, vence_en) "
        "VALUES (%s, %s, %s, now() + (%s || ' minutes')::interval)",
        (cod, cifrar(seis), mail[:200], str(MINUTOS_CODIGO)))
    return seis, mail


def _mail_de_asinfo(cod: str) -> str:
    """El correo del espejo de Asinfo.

    ⚠ Esa tabla se keyea por **`ruc10`** (los 10 primeros dígitos del RUC) y la
    columna se llama **`email`** — no `codigo_cli` ni `correo`. La escribe
    `modules/clientes/mail_asinfo.py` desde el catálogo de Asinfo, y es de
    donde sale que el 95% de la plata tiene correo cargado.

    Fail-soft: sin la tabla o sin RUC devuelve vacío, y el cliente cae en el
    camino de llamar por teléfono.
    """
    try:
        fic = cliente(cod) or {}
        r10 = ruc10(fic.get("ruc"))
        if not r10:
            return ""
        fila = db.fetch_one(
            "SELECT email FROM scintela.cliente_mail_asinfo WHERE ruc10 = %s",
            (r10,))
        return ((fila or {}).get("email") or "").strip()
    except Exception as e:  # noqa: BLE001 -- sin espejo, no hay mail y ya
        _LOG.warning("portal: no pude buscar el correo en Asinfo (%s)", e)
        return ""


def usar_codigo(codigo: str, seis: str) -> bool:
    """¿Este código de 6 dígitos sirve? Si sirve, lo quema.

    Se quema SIEMPRE que acierte, aunque después falle algo: un código de un
    solo uso que se puede reusar no es de un solo uso.
    """
    cod = normalizar_codigo(codigo)
    seis = re.sub(r"\D", "", seis or "")
    if not cod or len(seis) != 6:
        return False
    filas = db.fetch_all(
        "SELECT id_portal_codigo, codigo_hash FROM scintela.portal_codigo "
        " WHERE UPPER(TRIM(codigo_cli)) = %s AND usado_en IS NULL "
        "   AND vence_en > now() "
        " ORDER BY id_portal_codigo DESC LIMIT 5", (cod,)) or []
    for f in filas:
        if coincide(seis, f.get("codigo_hash")):
            db.execute("UPDATE scintela.portal_codigo SET usado_en = now() "
                       " WHERE id_portal_codigo = %s", (f["id_portal_codigo"],))
            # Destraba: el que llegó hasta acá probó su correo, no su memoria.
            db.execute(
                "UPDATE scintela.portal_acceso "
                "   SET intentos_fallidos = 0, bloqueado_hasta = NULL "
                " WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))
            return True
    return False


def cortar(codigo: str, quien: str) -> None:
    """El vendedor le cierra el acceso. NO borra la fila: queda el rastro."""
    db.execute(
        "UPDATE scintela.portal_acceso "
        "   SET activo = false, cortado_por = %s, cortado_en = now() "
        " WHERE UPPER(TRIM(codigo_cli)) = %s",
        ((quien or "")[:60], normalizar_codigo(codigo)))
