"""Las FICHAS TÉCNICAS de las telas, adentro de Programa Core (13/10/2026).

Hasta acá la pestaña "F. técnica" del portal llevaba a un programa APARTE (en
Render, con su propia clave): el vendedor salía de Mi Cartera y le costaba
volver (Tamara: *"se van y es difícil volver"*). Ese programa no es nuestro, y
meterlo en un iframe no sirve (su sesión no sobrevive adentro de otra página;
en iPhone, nunca). Decisión de Tamara: **copiar las 90 fichas acá** y que se
editen desde la oficina (opción "a": copia única, el sitio de afuera no se
vuelve a leer — sólo se compara a mano una vez por mes, tres meses).

Qué vive dónde:

⭐ Lo que CAMBIA de tela a tela está en `scintela.ficha_tecnica` (mig 0262):
   nombre, código FT-…, tubular/abierto, grupo, revisión, la tabla de
   especificaciones y las instrucciones de lavado.

⭐ Lo que es IGUAL en las 90 (observaciones técnicas, compromiso ambiental,
   firmas) vive ACÁ abajo, una sola vez. Medido el 13/10/2026 en los 90 .docx:
   una sola variante de cada uno.

⭐ El SÍMBOLO de cada instrucción de lavado sale del TEXTO (palabras clave), no
   de una columna: así una instrucción editada a mano sigue llevando su dibujo,
   y una que no se reconoce va sin dibujo en vez de con uno equivocado. Los
   dibujos son los mismos PNG de las fichas originales (static/fichas/).

Todo es fail-soft: si la base no contesta, la lista sale vacía con un aviso,
nunca un 500 delante del cliente.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

import db

_LOG = logging.getLogger("programa_core.mi_cartera.fichas")

SECCIONES = ("tubular", "abierto")

#: Orden de los grupos en la lista: el mismo que tenía el catálogo original.
ORDEN_GRUPOS = ("Fleece", "Jersey", "Pique", "Ribb", "Topper", "Otras")

OBSERVACIONES = (
    "Los colores pueden presentar ligeras diferencias de tono entre baños, "
    "dentro de la tolerancia de la normativa aplicable.",
    "El control de tonos debe ser efectuado por baños para evitar diferencias "
    "en el producto final.",
    "Almacenar los rollos de tela de forma horizontal, para evitar diferencias "
    "en anchos.",
    "Almacenar en sombra, sin exposición directa a luz solar o artificial "
    "intensa, ya que puede generar manchas por mal almacenamiento.",
    "Se recomienda rotar la mercadería expuesta en percheros.",
    "Productos estampados pueden presentar menores solideces.",
    "El porcentaje de Algodón indica el porcentaje de degradabilidad del "
    "producto.",
)

COMPROMISO = (
    "Intela Cía. Ltda. fabrica con responsabilidad ambiental. Contamos con "
    "planta de tratamiento de aguas residuales, gestión responsable de desechos "
    "y procesos de menor impacto ambiental. Usamos las mejores marcas de "
    "colorantes para garantizar alta solidez y minimizar la contaminación "
    "hídrica. El algodón utilizado es biodegradable y el porcentaje de algodón "
    "indica el nivel de degradabilidad del producto."
)

ELABORADO = ("Ing. Oscar Canchig G.", "Depto. Técnico de Calidad")
APROBADO = ("Gerencia Técnica", "Intela Cía. Ltda.")

#: (palabras que tienen que estar TODAS, archivo). El primero que calza gana,
#: por eso "no usar secadora" va antes que "secad".
_SIMBOLOS = (
    (("remojo",), "remojo.png"),
    (("lavado", "30"), "lavado30.png"),
    (("colores",), "colores.png"),
    (("clor",), "cloro.png"),
    (("exprimir",), "exprimir.png"),
    (("tendedero",), "tendedero.png"),
    (("no usar secadora",), "no_secadora.png"),
    (("secad",), "secado60.png"),
    (("planchar",), "planchar110.png"),
)


def sin_tildes(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def simbolo(texto: str) -> str | None:
    """El dibujo de una instrucción de lavado, o None si no se reconoce."""
    t = sin_tildes(texto)
    for palabras, archivo in _SIMBOLOS:
        if all(p in t for p in palabras):
            return archivo
    return None


def clave_tela(nombre: str) -> str:
    """Para cruzar el nombre de una tela de Asinfo con el de la ficha:
    sin tildes, sin mayúsculas, sin espacios ni puntuación ("Fleece 2.20" ==
    "FLEECE 2.20" == "fleece2.20")."""
    return re.sub(r"[^a-z0-9.]", "", sin_tildes(nombre))


def _armar(f: dict) -> dict:
    spec = f["spec"] if isinstance(f["spec"], list) else json.loads(f["spec"] or "[]")
    lav = f["lavado"] if isinstance(f["lavado"], list) else json.loads(f["lavado"] or "[]")
    # Agrupa la tabla por DESCRIPCIÓN (la celda que en la hoja ocupa varias
    # filas): [("COMPOSICIÓN", [(param, unidad, valor), …]), …]
    bloques: list[tuple[str, list]] = []
    for fila in spec:
        desc, param, unidad, valor = (list(fila) + ["", "", "", ""])[:4]
        if not bloques or (desc and desc != bloques[-1][0]):
            bloques.append((desc, []))
        bloques[-1][1].append((param, unidad, valor))
    # El renglón chico de la lista: "65% Poliéster · 35% Algodón" y
    # "288 g/m² · 102 cm · 1,70 m/kg" — sin las tolerancias, que se ven
    # en la ficha. Es lo que el vendedor busca con el cliente adelante.
    def _val(v: str) -> str:
        return (v or "").split("±")[0].split()[0] if (v or "").strip() else ""
    comp, car = [], []
    for desc, filas in bloques:
        d = sin_tildes(desc)
        for param, unidad, valor in filas:
            if d.startswith("composic") and _val(valor):
                comp.append(f"{_val(valor)}% {param}")
            elif d.startswith("caracter") and _val(valor):
                car.append(f"{_val(valor)} {unidad}")
    return {
        "resumen_comp": " · ".join(comp),
        "resumen_car": " · ".join(car),
        **{k: f[k] for k in ("codigo", "nombre", "seccion", "grupo", "revision",
                             "emision", "orden", "activa")},
        "actualizado_en": f.get("actualizado_en"),
        "actualizado_por": f.get("actualizado_por"),
        "spec": spec,
        "bloques": bloques,
        "lavado": [{"texto": t, "simbolo": simbolo(t),
                    "img": f"fichas/{simbolo(t)}" if simbolo(t) else None} for t in lav],
        "buscar": sin_tildes(f"{f['nombre']} {f['codigo']} {f['grupo']}"),
    }


def _orden(f: dict) -> tuple:
    g = f["grupo"]
    return (SECCIONES.index(f["seccion"]) if f["seccion"] in SECCIONES else 9,
            ORDEN_GRUPOS.index(g) if g in ORDEN_GRUPOS else len(ORDEN_GRUPOS),
            f["orden"], f["nombre"])


def lista(*, todas: bool = False) -> list[dict] | None:
    """Las fichas activas (o todas, para la oficina), en el orden del catálogo.
    None si la base no contestó — la pantalla lo dice."""
    try:
        filas = db.fetch_all(
            "SELECT * FROM scintela.ficha_tecnica"
            + ("" if todas else " WHERE activa"))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("fichas técnicas: %s", e)
        return None
    return sorted((_armar(f) for f in filas), key=_orden)


def una(codigo: str) -> dict | None:
    try:
        f = db.fetch_one("SELECT * FROM scintela.ficha_tecnica WHERE codigo = %s",
                         ((codigo or "").strip(),))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("ficha técnica %s: %s", codigo, e)
        return None
    return _armar(f) if f else None


def por_tela() -> dict[str, str]:
    """{clave_tela(nombre): código} de las activas — para el link 📄 de Stock."""
    return {clave_tela(f["nombre"]): f["codigo"] for f in (lista() or [])}


# ── Edición (oficina) ────────────────────────────────────────────────────────

_RE_CODIGO = re.compile(r"^FT-[A-Z0-9.\-]{1,36}$")


def leer_form(form) -> dict:
    """Valida lo que vino del formulario de edición. Tira ValueError con un
    mensaje para la pantalla si algo no sirve."""
    nombre = " ".join((form.get("nombre") or "").split()).upper()
    if not nombre:
        raise ValueError("Falta el nombre de la tela.")
    seccion = (form.get("seccion") or "").strip().lower()
    if seccion not in SECCIONES:
        raise ValueError("Elegí Tubular o Abierto.")
    grupo = " ".join((form.get("grupo") or "").split()) or "Otras"
    revision = (form.get("revision") or "").strip() or "01"
    emision = " ".join((form.get("emision") or "").split())
    if not emision:
        raise ValueError("Falta la fecha de emisión (por ejemplo: Julio 2026).")
    spec = []
    for desc, param, unidad, valor in zip(form.getlist("s_desc"), form.getlist("s_param"),
                                          form.getlist("s_unidad"), form.getlist("s_valor"),
                                          strict=False):
        fila = [" ".join((x or "").split()) for x in (desc, param, unidad, valor)]
        if fila[1] or fila[3]:  # renglón vacío = renglón borrado
            fila[0] = fila[0].upper()
            spec.append(fila)
    if not spec:
        raise ValueError("La tabla de especificaciones quedó vacía.")
    # La descripción vacía hereda la de arriba (como la celda combinada).
    for i in range(1, len(spec)):
        if not spec[i][0]:
            spec[i][0] = spec[i - 1][0]
    if not spec[0][0]:
        raise ValueError("El primer renglón necesita su descripción (COMPOSICIÓN…).")
    lavado = [" ".join(t.split()) for t in (form.get("lavado") or "").splitlines()]
    lavado = [t for t in lavado if t]
    return {"nombre": nombre, "seccion": seccion, "grupo": grupo,
            "revision": revision, "emision": emision, "spec": spec,
            "lavado": lavado, "activa": bool(form.get("activa"))}


def guardar(codigo: str, datos: dict, *, por: str, nueva: bool = False) -> None:
    """Graba la ficha. `nueva` la da de alta (el código no puede existir)."""
    codigo = (codigo or "").strip().upper()
    if not _RE_CODIGO.match(codigo):
        raise ValueError("El código tiene que empezar con FT- (por ejemplo FT-FL-102).")
    params = (datos["nombre"], datos["seccion"], datos["grupo"], datos["revision"],
              datos["emision"], json.dumps(datos["spec"], ensure_ascii=False),
              json.dumps(datos["lavado"], ensure_ascii=False), datos["activa"], por)
    if nueva:
        if db.fetch_one("SELECT 1 AS x FROM scintela.ficha_tecnica WHERE codigo = %s",
                        (codigo,)):
            raise ValueError(f"Ya hay una ficha con el código {codigo}.")
        db.execute(
            """
            INSERT INTO scintela.ficha_tecnica
                (nombre, seccion, grupo, revision, emision, spec, lavado, activa,
                 actualizado_por, codigo, orden)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
                    (SELECT COALESCE(MAX(orden), 0) + 10 FROM scintela.ficha_tecnica))
            """, (*params, codigo))
        return
    n = db.execute(
        """
        UPDATE scintela.ficha_tecnica
           SET nombre = %s, seccion = %s, grupo = %s, revision = %s, emision = %s,
               spec = %s::jsonb, lavado = %s::jsonb, activa = %s,
               actualizado_por = %s, actualizado_en = now()
         WHERE codigo = %s
        """, (*params, codigo))
    if not n:
        raise LookupError(codigo)
