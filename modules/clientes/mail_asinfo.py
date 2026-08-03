"""El MAIL del cliente — de dónde sale y quién le gana a quién.

⭐ POR QUÉ (TMT 2026-08-03). La dueña pidió ver el mail en el estado de cuenta.
Al ir a buscarlo apareció que en Programa Core **no existe**: `cliente.correo`
estaba cargado en **1** cliente de 3.973. Los mails se venían tipeando en el
campo **Observación** del dBase — 2.984 los tienen ahí, pegados a notas
sueltas (`"ventas@americanspirit.ec MBT"`, `"isabel1981@yahoo.es  CONTADO"`).

Y ese campo es **CHAR(30)**: **225 mails llegan cortados** justo a los 30
caracteres. Lo peor no son los que se ven rotos, son los que **parecen
válidos**: `contabilidadnortextil@gmail.co` parsea como un mail correcto y no
lo es. Mostrarlo sin más es peor que no mostrar nada.

La dueña preguntó si se podían adivinar. Se puede, pero **casi nunca hace
falta**: Asinfo (el ERP) le manda la factura electrónica al cliente por mail,
así que tiene el mail **real y completo** de ~3.500 clientes. De los 225
cortados, Asinfo **confirma exactamente** 98 (su mail arranca con el fragmento
que quedó en la Observación). Eso no es adivinar: es completar.

## Las dos trampas de la data de Asinfo

1. **811 clientes tienen cargado el mail de la PROPIA INTELA**
   (`facturaelectronica@intela.com.ec` 333, `hbintela@hotmail.com` 332,
   `contabilidad@intela.com.ec` 146). Es el relleno de facturación
   electrónica para el cliente que no dio mail. Tomarlos como "el mail del
   cliente" haría que el estado de cuenta se le mande a Intela misma. Se
   filtran ACÁ, al ingerir, para que no entren nunca a Programa Core.
2. **En 86 casos Asinfo tiene un mail DISTINTO** al de la Observación
   (`contabilidad@` vs `ventas@` de la misma empresa, y algún caso que parece
   otra empresa). Decisión de la dueña: **se muestran los dos**, nunca se
   elige por ella.

## Por qué una tabla espejo y no consulta en vivo

Asinfo se alcanza sólo por Metabase (ver skill `programa-core-integraciones`),
que tarda segundos. El estado de cuenta se abre todo el día: una llamada por
carga sería inaceptable, y con Metabase caído la pantalla quedaría colgada.
Se copia el catálogo a `scintela.cliente_mail_asinfo` y las pantallas leen
local. Lo refresca el cron diario de `/admin/health/all`, igual que las
retenciones. Bootstrap `CREATE TABLE IF NOT EXISTS` en caliente porque **el
deploy no corre migraciones**.

**No escribe `cliente.correo`.** Pedido de la dueña: "solo los vacíos". Asinfo
rellena el hueco en pantalla; lo que alguien cargó a mano no se pisa nunca.
"""
from __future__ import annotations

import logging
import re

import db as _db

_LOG = logging.getLogger("programa_core.clientes.mail_asinfo")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.cliente_mail_asinfo (
    ruc10         TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    nombre_fiscal TEXT,
    actualizado   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Mail bien formado. Sin `%` en la clase de caracteres: estas cadenas viajan
# por psycopg2 con parámetros y el signo se toma como placeholder.
# Ver [[feedback_porcentaje_suelto_en_sql]] — ya tiró producción abajo una vez.
RE_MAIL = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}")

#: Lo mismo pero sin exigir dominio válido — para reconocer los fragmentos a
#: los que el corte de 30 caracteres les comió el final.
RE_FRAGMENTO = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]*")

# El largo del campo OBS del dBase. Un mail que termina EXACTO en el
# caracter 30 está cortado, aunque parezca válido.
LARGO_OBSERVACION = 30

#: Mails de la propia Intela usados como relleno en Asinfo. Comparación
#: EXACTA y por dominio — nunca "contiene", que se comería mails legítimos
#: de un cliente que trabaje con intela.com.ec.
DOMINIO_CASA = "intela.com.ec"
MAILS_CASA = frozenset({"hbintela@hotmail.com"})


def es_mail_de_la_casa(mail: str) -> bool:
    """True si el mail es de Intela, no del cliente."""
    m = (mail or "").strip().lower()
    if not m:
        return False
    return m in MAILS_CASA or m.endswith("@" + DOMINIO_CASA)


def ruc10(valor: str | None) -> str:
    """Clave de cruce PC↔Asinfo: los primeros 10 dígitos del RUC.

    En Ecuador el RUC de persona natural es la cédula (10) + '001'. PC guarda
    a veces la cédula pelada y Asinfo casi siempre el RUC completo, así que
    los 10 primeros dígitos son lo único comparable. La cédula es única, así
    que no genera colisiones.
    """
    solo_digitos = re.sub(r"[^0-9]", "", valor or "")
    return solo_digitos[:10] if len(solo_digitos) >= 10 else ""


# ---------------------------------------------------------------------------
# Resolución del mail a mostrar — función PURA, testeable sin Postgres
# ---------------------------------------------------------------------------


def mail_de_observacion(observacion: str | None) -> tuple[str, bool]:
    """Devuelve (mail, cortado) leyendo el campo Observación del cliente.

    `cortado` es True cuando la Observación llegó al tope de 30 caracteres y
    el mail termina justo ahí: en ese caso lo que se leyó es un **fragmento**,
    no un mail, aunque el parser lo dé por bueno (`...@gmail.co`).
    """
    obs = observacion or ""
    m = RE_MAIL.search(obs)
    if m:
        mail = m.group(0)
        cortado = len(obs) == LARGO_OBSERVACION and obs.rstrip().endswith(mail)
        return mail, cortado
    # Sin mail bien formado. Puede seguir habiendo un fragmento: son los cortes
    # más brutales, donde el tijeretazo se comió el dominio entero
    # (`contabilidad@textilesalvarez.c`, `...@hotmail.`). Se devuelven igual,
    # marcados como cortados, para que la pantalla los pueda mostrar como
    # "quedó a medias" en vez de tragárselos en silencio.
    f = RE_FRAGMENTO.search(obs)
    if f and len(obs) == LARGO_OBSERVACION and obs.rstrip().endswith(f.group(0)):
        return f.group(0), True
    return "", False


def resolver(
    correo_ficha: str | None,
    observacion: str | None,
    mail_asinfo: str | None,
) -> dict:
    """Qué mail se muestra, de dónde salió, y si hay un segundo distinto.

    Prioridad (decisión de la dueña 2026-08-03, "solo los vacíos"):
      1. `cliente.correo` — lo que alguien cargó a mano en la ficha. Nunca
         se pisa: es el único campo editable por la UI.
      2. El mail de la Observación, **si está completo**.
      3. Asinfo — rellena el hueco.

    Un fragmento cortado NO se muestra como si fuera un mail. Si Asinfo tiene
    uno que **empieza con ese fragmento**, es el mismo mail completo y se usa
    (origen `asinfo_completa`). Si Asinfo tiene otro distinto, se muestran los
    dos y decide la persona.
    """
    ficha = (correo_ficha or "").strip()
    asinfo = (mail_asinfo or "").strip()
    if es_mail_de_la_casa(asinfo):
        asinfo = ""
    obs, cortado = mail_de_observacion(observacion)

    def _r(principal, origen, alternativo="", origen_alt="", incompleto="",
           sugerido=""):
        return {
            "mail": principal,
            "origen": origen,
            # Deducción, no dato: la pantalla la muestra aparte y marcada.
            "sugerido": sugerido,
            # La etiqueta se arma acá y no en el Jinja: así el template no
            # necesita un filtro nuevo y el texto se testea con el resto.
            "etiqueta": ETIQUETA_ORIGEN.get(origen, ""),
            "alternativo": alternativo,
            "origen_alt": origen_alt,
            "incompleto": incompleto,
        }

    if ficha:
        alt = asinfo if asinfo and asinfo.lower() != ficha.lower() else ""
        return _r(ficha, "ficha", alt, "asinfo" if alt else "")

    if obs and not cortado:
        alt = asinfo if asinfo and asinfo.lower() != obs.lower() else ""
        return _r(obs, "observacion", alt, "asinfo" if alt else "")

    if cortado:
        if asinfo and asinfo.lower().startswith(obs.lower()):
            # Mismo mail, completo. No es una suposición.
            return _r(asinfo, "asinfo_completa")
        if asinfo:
            # Asinfo tiene otro contacto: se muestra, y el fragmento queda a
            # la vista para que se note que hay algo a medio cargar.
            return _r(asinfo, "asinfo", "", "", incompleto=obs)
        # Último recurso: deducir el final del dominio. Va como SUGERENCIA.
        return _r("", "cortado", "", "", incompleto=obs, sugerido=deducir(obs))

    if asinfo:
        return _r(asinfo, "asinfo")
    return _r("", "")


# ---------------------------------------------------------------------------
# Deducir el final que se comió el corte — SOLO donde no hay ambigüedad
# ---------------------------------------------------------------------------

#: Proveedores donde el final es predecible. Los números salen de contar los
#: dominios de los 2.759 mails COMPLETOS de esta cartera (03/08):
#:   hotmail.com 1199 vs hotmail.es 91  → 93 %
#:   gmail.com    917 vs gmail.es    0  → 100 %
#:   outlook.com   57 vs outlook.es  19 → 75 %  (queda afuera, ver abajo)
#: ⛔ **yahoo NO se deduce**: 139 .com contra 85 .es. Ahí la moneda sale cara
#: y cruz, y un mail mal deducido le manda el estado de cuenta a un
#: desconocido. Preferimos el "—" antes que inventar.
_DEDUCIBLES = (
    # (final que quedó tras el corte, mail completo)
    (re.compile(r"@gmai?l?[.]?c?o?m?$", re.I), "@gmail.com"),
    (re.compile(r"@gmail[.]?c?o?$", re.I), "@gmail.com"),
    (re.compile(r"@hotmai?l?[.]?c?o?m?$", re.I), "@hotmail.com"),
    (re.compile(r"@hotmailc?$", re.I), "@hotmail.com"),
    (re.compile(r"@hotm[.]?c?o?m?$", re.I), "@hotmail.com"),
    (re.compile(r"@hm(al)?[.]?c?o?m?$", re.I), "@hotmail.com"),
    (re.compile(r"@live[.]?c?o?$", re.I), "@live.com"),
)


def deducir(fragmento: str) -> str:
    """Completa el final de un mail cortado, o "" si no es seguro.

    Dueña 2026-08-03: *"puedes intentar adivinar los mails que faltan? ejemplo
    este sabemos que es fabricadecamisetaslex@hotmail.com"*. Se puede cuando
    lo único que falta es el final de un dominio masivo y no hay otra opción
    razonable. **Nunca** cuando el corte se comió parte del nombre o el
    dominio es propio (`...@cmdpublicidadte`): ahí no hay nada que deducir,
    hay que ir a preguntarle al cliente.

    Lo que devuelve es una SUGERENCIA, y la pantalla lo muestra como tal.
    """
    f = (fragmento or "").strip().lower()
    if f.count("@") != 1:
        return ""
    local, _, _dominio = f.partition("@")
    if not local:
        return ""
    for regex, completo in _DEDUCIBLES:
        if regex.search(f):
            candidato = regex.sub(completo, f)
            return candidato if RE_MAIL.fullmatch(candidato) else ""
    return ""


ETIQUETA_ORIGEN = {
    "ficha": "Cargado en la ficha del cliente",
    "observacion": "Tomado de la Observación del cliente",
    "asinfo": "Tomado de Asinfo (facturación electrónica)",
    "asinfo_completa": (
        "Tomado de Asinfo — completa el mail que en la Observación "
        "entró cortado por el límite de 30 caracteres"
    ),
    "cortado": "En la Observación quedó cortado y Asinfo no lo tiene",
}


# ---------------------------------------------------------------------------
# Ingesta desde Asinfo
# ---------------------------------------------------------------------------

_SQL_ASINFO = """
SELECT LEFT(identificacion, 10) AS ruc10,
       MAX(nombre_fiscal)       AS nombre_fiscal,
       MAX(email1)              AS email
  FROM empresa
 WHERE indicador_cliente = 1
   AND LEN(LTRIM(RTRIM(ISNULL(email1, '')))) > 3
   AND LEN(LTRIM(RTRIM(ISNULL(identificacion, '')))) >= 10
 GROUP BY LEFT(identificacion, 10)
"""


_bootstrapped = False


def asegurar_tabla() -> None:
    """Crea la tabla espejo si falta. Idempotente y a prueba de fallos.

    La llama el estado de cuenta ANTES de consultar, porque su query hace
    LEFT JOIN contra esta tabla: si no existiera, la pantalla entera se caería
    con "relation does not exist" — y el deploy no corre migraciones.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("bootstrap de cliente_mail_asinfo falló: %s", exc)
    _bootstrapped = True


def refrescar() -> dict:
    """Copia el catálogo de mails de Asinfo a la tabla espejo. Idempotente.

    Fail-soft: si Metabase no está configurado o se cayó, NO borra ni pisa lo
    que ya está guardado — deja la última foto buena y avisa. Vaciar la tabla
    porque el puente falló dejaría a todos los clientes sin mail de golpe.
    """
    from modules._lib import metabase_client as mc

    asegurar_tabla()
    if not mc.disponible():
        return {"ok": False, "error": "Metabase no configurado", "filas": 0}

    filas, contesto = mc.fetch_dataset_estado(2, _SQL_ASINFO, max_results=20000)
    if not contesto:
        return {"ok": False, "error": "Metabase no contestó", "filas": 0}

    descartadas_casa = 0
    valores: list[tuple[str, str, str]] = []
    vistos: set[str] = set()
    for f in filas or []:
        clave = ruc10(f.get("ruc10"))
        email = (f.get("email") or "").strip().lower()
        if not clave or not email or not RE_MAIL.fullmatch(email):
            continue
        if es_mail_de_la_casa(email):
            descartadas_casa += 1
            continue
        if clave in vistos:
            # Un mismo RUC10 puede venir dos veces (RUC y cédula del mismo
            # titular). El ON CONFLICT no salva de duplicados DENTRO del
            # mismo INSERT: Postgres tira "cannot affect row a second time".
            continue
        vistos.add(clave)
        valores.append((clave, email, (f.get("nombre_fiscal") or "")[:120]))

    if not valores:
        return {"ok": True, "filas": 0, "descartadas_casa": descartadas_casa,
                "leidas_de_asinfo": len(filas or [])}

    # UN solo INSERT. La primera versión hacía uno por fila: 3.500 viajes a
    # RDS dentro del request del cron → /admin/health/all devolvía 502 por
    # timeout del proxy. TMT 2026-08-03.
    marcadores = ",".join(["(%s, %s, %s)"] * len(valores))
    planos: list[str] = [v for fila in valores for v in fila]
    _db.execute(
        f"""
        INSERT INTO scintela.cliente_mail_asinfo (ruc10, email, nombre_fiscal)
             VALUES {marcadores}
        ON CONFLICT (ruc10) DO UPDATE
                SET email = EXCLUDED.email,
                    nombre_fiscal = EXCLUDED.nombre_fiscal,
                    actualizado = CURRENT_TIMESTAMP
        """,
        tuple(planos),
    )
    return {
        "ok": True,
        "filas": len(valores),
        "descartadas_casa": descartadas_casa,
        "leidas_de_asinfo": len(filas or []),
    }


#: El espejo cambia de a poco (un mail nuevo por semana). Refrescarlo más de
#: una vez por día es gastar el puente de Metabase al pedo.
HORAS_FRESCO = 20


def esta_fresco() -> bool:
    try:
        asegurar_tabla()
        r = _db.fetch_one(
            """
            SELECT MAX(actualizado) AS ult, COUNT(*) AS n
              FROM scintela.cliente_mail_asinfo
            """
        )
    except Exception:  # noqa: BLE001
        return False
    if not r or not r.get("ult") or not r.get("n"):
        return False
    from datetime import datetime, timedelta
    return r["ult"] > datetime.now() - timedelta(hours=HORAS_FRESCO)


def refrescar_cron() -> dict:
    """Envoltorio para /admin/health/all — nunca levanta y no repite trabajo.

    El cron corre una vez al día, pero `/admin/health/all` lo abre cualquiera
    desde el panel: sin el guard de frescura, cada visita dispararía la
    consulta pesada a Asinfo y agregaría segundos al request.
    """
    try:
        if esta_fresco():
            return {"ok": True, "salteado": "ya está fresco", "filas": 0}
        return refrescar()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("refresco de mails de Asinfo falló: %s", e)
        return {"ok": False, "error": str(e)[:200], "filas": 0}
