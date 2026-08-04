"""El CSS de celular no puede tocar el escritorio, ni quedar colgado de un
gancho que ya no existe.

TMT 2026-08-04. La dueña pidió ver /informes/balance (y sus 3 pantallas
hermanas) desde el teléfono, con la condición explícita de "no cambiar nada de
como se ve en la compu". La forma de garantizarlo es que TODO el CSS nuevo viva
adentro de un `@media (max-width: 767px)`: arriba de 768px no aplica nada.

Los dos tests de acá cubren las dos formas conocidas de romper eso:

1. Que alguien agregue una regla al bloque y se le escape del @media
   (→ pisa el escritorio sin que nadie lo note hasta que la dueña lo ve).
2. Que alguien renombre o borre el `id`/`class` que la regla usa de gancho
   (→ el CSS sigue ahí, prolijo, y no hace NADA; es el mismo modo de falla
   silenciosa del `stat_select` del 03/08: sin error, sin flash, sin nada).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
BASE_HTML = RAIZ / "templates" / "base.html"

INICIO = "══════════════════ CELULAR — inicio"
FIN = "══════════════════ CELULAR — fin"


def _bloque_celular() -> str:
    css = BASE_HTML.read_text(encoding="utf-8")
    # arrancamos en el `/*` que abre el comentario del encabezado, para que el
    # bloque quede con los comentarios balanceados y se puedan sacar enteros.
    i = css.rindex("/*", 0, css.index(INICIO))
    f = css.rindex("/*", 0, css.index(FIN))
    return css[i:f]


def _templates() -> list[Path]:
    """Todos los .html del proyecto (templates/ raíz + los de cada módulo)."""
    return [
        p
        for p in RAIZ.rglob("*.html")
        if ".venv" not in p.parts and "node_modules" not in p.parts
    ]


def test_todo_el_css_de_celular_vive_adentro_del_media_query():
    """Ni una regla del bloque puede quedar afuera del @media.

    Excepción única y deliberada: `.solo-movil { display: none; }`, que es el
    default de escritorio (esconde el texto abreviado). Si eso se moviera
    adentro del @media, el escritorio mostraría las dos versiones del título.
    """
    bloque = _bloque_celular()

    # Sacamos comentarios /* … */ para no leer llaves de la prosa.
    sin_comentarios = re.sub(r"/\*.*?\*/", "", bloque, flags=re.S)

    medias = re.findall(r"@media\s*\(max-width:\s*767px\)", sin_comentarios)
    assert len(medias) == 1, "el bloque CELULAR tiene que ser UN solo @media"

    antes, _, adentro = sin_comentarios.partition("@media (max-width: 767px)")

    # Afuera sólo puede quedar el default de .solo-movil.
    reglas_afuera = [
        r.strip() for r in re.findall(r"([^{}]+\{[^{}]*\})", antes) if r.strip()
    ]
    assert len(reglas_afuera) == 1, f"reglas fuera del @media: {reglas_afuera}"
    assert reglas_afuera[0].startswith(".solo-movil")
    assert "display: none" in reglas_afuera[0]

    # Y el @media tiene que cerrar bien (llaves balanceadas).
    assert adentro.count("{") == adentro.count("}"), "llaves desbalanceadas"


def test_cada_gancho_del_css_de_celular_existe_en_algun_template():
    """Cada `#id` / `.clase` del bloque tiene que aparecer en algún template.

    Es el test que hubiera cazado el 404 de los links hardcodeados y el
    `select.onchange === null`: el CSS no falla ruidosamente, simplemente no
    aplica. Si mañana alguien renombra `.balance-col` o le saca el
    `id="app-shell"` al shell, este test se pone rojo en vez de dejar la
    pantalla del celu rota en silencio.
    """
    bloque = _bloque_celular()
    sin_comentarios = re.sub(r"/\*.*?\*/", "", bloque, flags=re.S)
    # Sólo los selectores (lo de antes de cada `{`), nunca las declaraciones.
    selectores = " ".join(re.findall(r"([^{}]+)\{", sin_comentarios))

    ganchos = set(re.findall(r"[.#]([A-Za-z_][\w-]*)", selectores))
    # `max-width` y compañía no son selectores; y los pseudo/elementos tampoco.
    ganchos -= {"first-child", "hover", "not"}

    # OJO: hay que buscar en el MARKUP, no en las hojas de estilo. Si dejamos
    # los <style> adentro, cada gancho se encuentra a sí mismo en el CSS que lo
    # declara y el test pasa siempre (pasó en la primera versión).
    html = "\n".join(
        re.sub(r"<style\b.*?</style>", "", p.read_text(encoding="utf-8"), flags=re.S)
        for p in _templates()
    )

    faltan = sorted(g for g in ganchos if not re.search(rf"\b{re.escape(g)}\b", html))
    assert not faltan, (
        "el CSS de celular usa ganchos que ya no existen en ningún template "
        f"(la regla no aplica nunca): {faltan}"
    )


def test_las_pantallas_de_informes_quedan_afuera_del_scroll_generico():
    """La regla genérica `main table { display: block }` NO puede alcanzarlas.

    En las listas (cheques, compras, clientes…) queremos que la tabla se panee
    de costado. En las 4 de informes queremos lo contrario: la tabla entera
    entra en 360px y ahí un scroll horizontal sería un bug, no una ayuda. Si
    alguien borra la línea de exclusión, Resultados vuelve a "cortarse" en el
    teléfono y no hay forma de notarlo mirando el escritorio.
    """
    bloque = re.sub(r"/\*.*?\*/", "", _bloque_celular(), flags=re.S)

    assert "main table { display: block;" in bloque, "cambió la regla genérica"

    reglas = re.findall(r"([^{}]+)\{([^{}]*)\}", bloque)
    excluidos = " ".join(
        sel for sel, cuerpo in reglas if re.search(r"display:\s*table\b", cuerpo)
    )
    for contenedor in (".informe-balance", ".h12-page", ".gastos-matriz", ".fp"):
        assert f"{contenedor} table" in excluidos, (
            f"{contenedor} quedó adentro del scroll genérico: su tabla ya entra "
            "completa en el celu y no debe panearse"
        )


@pytest.mark.parametrize("tpl", _templates(), ids=lambda p: p.name)
def test_solo_movil_y_solo_escritorio_van_siempre_de_a_pares(tpl: Path):
    """Un `.solo-movil` sin su `.solo-escritorio` = un título que desaparece.

    El patrón es SIEMPRE `<span class="solo-escritorio">Largo</span>` seguido de
    `<span class="solo-movil">Corto</span>`. Si alguien deja uno solo, el texto
    se pierde en una de las dos pantallas y nadie se entera hasta que falta un
    encabezado de columna.
    """
    html = tpl.read_text(encoding="utf-8")
    n_mov = len(re.findall(r'class="solo-movil"', html))
    n_esc = len(re.findall(r'class="solo-escritorio"', html))
    assert n_mov == n_esc, f"{tpl.name}: {n_esc} solo-escritorio vs {n_mov} solo-movil"

    # y cada par tiene que estar pegado (mismo <th>/<td>, no en otra parte).
    pares = re.findall(
        r'class="solo-escritorio">[^<]*</span>\s*<span class="solo-movil">', html
    )
    assert len(pares) == n_mov, f"{tpl.name}: hay solo-movil sueltos, sin su par"
