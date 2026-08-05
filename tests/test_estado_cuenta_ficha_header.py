"""La FICHA del encabezado del estado de cuenta (una sola tarjeta).

TMT 2026-08-04 (dueña, con la captura de IJM al lado): *"cuando veo el estado
de cuenta del cliente, el nombre y tantos botones sin diseño está todo feo, me
podés ayudar a crear algo simple elegante sin perder funcionalidad?"*.

Arriba de la primera factura había NUEVE bloques sueltos: el form de "nuevo
estado de cuenta" flotando solo, el nombre en un h1 grande que con "ISRAEL
JONNATHAN MIRANDA MADRI" se partía en tres renglones, SEIS botones outline del
mismo peso visual al lado (Volver / Imprimir todo / Imprimir facturas /
Imprimir cheques / WhatsApp / Totalizar) que se acomodaban en dos filas, la
grilla de contacto, una tira de pestañas con UNA sola pestaña, 4 tiles y 3
tiles más.

Rediseñar es fácil; rediseñar sin comerse un botón, no. **"sin perder
funcionalidad" es la mitad del pedido**, así que este archivo es sobre todo un
inventario: cada acción y cada dato que estaba antes tiene que seguir estando,
con su mismo permiso. Si mañana alguien vuelve a tocar el header, esto es lo
que avisa que se perdió algo.

Son tests de SOURCE (como test_estado_cuenta_totales.py y
test_estado_cuenta_vendedor_mail.py): en CI no hay Postgres y la pantalla no se
puede renderizar de verdad.
"""
from __future__ import annotations

import re
from pathlib import Path

_TPL_DIR = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes"
)
HTML = (_TPL_DIR / "estado_cuenta.html").read_text(encoding="utf-8")
# Sin los comentarios {# … #}: esos SÍ nombran los botones viejos (cuentan la
# historia del rediseño) y harían pasar por markup lo que es prosa.
SIN_COMENTARIOS = re.sub(r"\{#.*?#\}", "", HTML, flags=re.S)
BOTONES = (_TPL_DIR / "_ec_print_botones.html").read_text(encoding="utf-8")
LOTE = (_TPL_DIR / "estado_cuenta_lote_print.html").read_text(encoding="utf-8")


def _ficha() -> str:
    """La tarjeta entera, desde que abre hasta el cierre de la barra de acciones."""
    i = HTML.index('<div class="ec-ficha no-print">')
    j = HTML.index('class="ec-acciones"', i)
    return HTML[i:HTML.index("</div>", HTML.index("Totalizar (re-liquidar FIFO)", j))]


# ─────────────────────────── no se perdió nada ───────────────────────────


def test_estan_las_seis_acciones_que_habia_antes():
    """El inventario. Seis botones se volvieron una barra + un menú, pero los
    seis siguen llegando a la misma pantalla."""
    ficha = _ficha()
    for accion, donde in (
        ("← Cartera", "cartera.aging"),
        ("Editar cliente", "clientes.editar"),
        ("Totalizar (re-liquidar FIFO)", "informes.estado_cuenta_totalizar"),
    ):
        assert accion in ficha, f"se perdió «{accion}»"
        assert donde in ficha, f"«{accion}» ya no apunta a {donde}"
    # Las tres impresiones + WhatsApp llegan por los includes compartidos.
    assert "informes/_ec_print_botones.html" in ficha
    assert "informes/_ec_boton_whatsapp.html" in ficha


def test_las_tres_impresiones_siguen_saliendo_del_partial_compartido():
    """Los tres botones NO pueden ser tres <button> copiados: el papel de la
    pantalla individual y el del lote tienen que seguir definiéndose en un solo
    archivo. Ese fue el motivo de compartirlo el 03/08 — si un lado cambia y el
    otro no, la hoja sale distinta según de dónde se imprimió y no se nota
    hasta que está impresa."""
    assert "function ecImprimir" not in HTML, "la pantalla copió el helper"
    assert "ec-print-facturas" not in HTML, "la pantalla copió el CSS de papel"
    for corto, largo in (("Todo", "Imprimir todo"),
                         ("Facturas", "Imprimir facturas"),
                         ("Cheques", "Imprimir cheques")):
        assert f">{corto}<" not in SIN_COMENTARIOS, f"«{corto}» quedó hardcodeado"
        assert largo not in SIN_COMENTARIOS, f"«{largo}» quedó hardcodeado"
        assert f"'{corto}' if ec_btn_corto else '{largo}'" in BOTONES


def test_las_opciones_van_detras_de_un_icono_de_impresora():
    """Dueña 04/08: *"imprimir que ya estén los botones. Pone el ícono de
    impresora y luego cada opción, así, se entiende?"*.

    Antes probamos un menú "Imprimir ▾" que colapsaba las tres opciones:
    ahorraba ancho pero costaba un click y escondía que existen TRES hojas
    distintas. Con el ícono adelante el grupo se lee de un vistazo, y por eso
    los rótulos van cortos — repetir "Imprimir" tres veces al lado de un dibujo
    de impresora es ruido.
    """
    ficha = _ficha()
    assert "<details" not in ficha, "volvió el menú desplegable"
    i_ico = ficha.index('class="ec-print-ico"')
    i_inc = ficha.index("_ec_print_botones.html")
    assert i_ico < i_inc, "el ícono va ANTES de las opciones"
    assert "<svg" in ficha[i_ico:i_inc], "el ícono de impresora no se dibuja"
    assert "ec_btn_corto = true" in ficha[:i_inc], "faltan los rótulos cortos"
    # Un solo control segmentado, no tres botones sueltos como en la vieja.
    assert '<span class="ec-print">' in ficha
    assert ".ec-print {" in HTML and "border-radius: 8px" in HTML


def test_el_menu_se_dibuja_con_la_clase_que_le_pasa_la_pantalla():
    """`ec_btn_clase` y `ec_btn_corto` son los únicos grados de libertad del
    partial: la pantalla los quiere pegados detrás de un ícono y con rótulo
    corto, el lote como botones sueltos con el rótulo largo."""
    ficha = _ficha()
    i = ficha.index("_ec_print_botones.html")
    assert "ec_btn_clase = 'ec-print-b'" in ficha[:i], (
        "el include no le pasa la clase de los botones del grupo"
    )
    assert BOTONES.count("ec_btn_clase | default(") == 3, (
        "los tres botones tienen que aceptar la clase, y con default"
    )


def test_el_lote_no_cambio_de_aspecto():
    """El lote NO pasa `ec_btn_clase` → cae en el default, que es exactamente
    las clases que tenía antes. Un rediseño de la pantalla individual no puede
    cambiarle los botones a la impresión por vendedor / provincia / grupos."""
    assert "ec_btn_clase" not in LOTE and "ec_btn_corto" not in LOTE
    assert "text-sm px-3 py-1.5 rounded border border-slate-300" in BOTONES


def test_los_cuatro_numeros_siguen_estando():
    ficha = _ficha()
    for etiqueta in ("Saldo facturas", "Cheques por cobrar",
                     "Total (facturas + cheques)", "Cupo"):
        assert f">{etiqueta}</div>" in ficha, f"se perdió el número «{etiqueta}»"


# Los rótulos, tal cual salen en pantalla. "Dirección (2)" es la segunda línea
# de dirección del dBase; "Tel" y "Mail" van pegados y en ese orden.
CAMPOS = ("Dirección:", "Dirección (2):", "Ciudad:", "RUC:", "Tel:", "Mail:",
          "Vendedor:", "Forma de pago:", "Descuento:", "Stop:")


def _grilla() -> str:
    """El bloque `.ec-datos` — los datos del cliente, en dos columnas."""
    abre = '<div class="ec-datos">'
    i = HTML.index(abre) + len(abre)
    return HTML[i:HTML.index("{# — Los cuatro números", i)]


def test_estan_los_diez_datos_del_cliente():
    """⭐ La corrección del 04/08. La primera versión puso Forma de pago,
    Descuento y Stop como CHIPS de 11 px al lado del nombre. La dueña miró y
    dijo *"falta crédito, descuento"*; cuando le mostré que sí estaban, *"ah sí
    está pero no me gusta"*. Un dato que hay que cazar está, pero no se ve.
    """
    grilla = _grilla()
    for campo in CAMPOS:
        assert f'<span class="ec-lbl">{campo}</span>' in grilla, f"falta «{campo}»"


def test_el_bloque_de_contacto_es_el_compacto_de_la_pantalla_vieja():
    """TMT 2026-08-04, tercera vuelta: la dueña mandó el recorte del bloque de
    contacto ANTERIOR — *"me gusta más esta parte de la antigua"*.

    La segunda versión lo había pasado a rótulo-ARRIBA / valor-abajo (el patrón
    de `admin_dbase/clientes_asinfo_detalle`). Se veía prolijo y ocupaba el
    doble de alto: "Dirección: CORAZON 199" en una sola línea se barre de un
    vistazo, partido en dos obliga a saltar con la vista en cada campo.

    Este test fija esa decisión: rótulo INLINE (`<span class="ec-lbl">Campo:`)
    y no un rótulo en su propia línea.
    """
    grilla = _grilla()
    assert '<div class="l">' not in grilla, (
        "volvió el rótulo arriba / valor abajo; la dueña pidió el compacto"
    )
    for campo in CAMPOS:
        i = grilla.index(f'>{campo}</span>')
        # Después del rótulo viene el valor en la MISMA línea, no un <div>.
        assert "<div" not in grilla[i:grilla.index("\n", i)], f"«{campo}» partió en dos líneas"


def test_ningun_campo_desaparece_cuando_esta_vacio():
    """La otra mitad del error de la primera versión: los chips colgaban de un
    `{% if %}`, así que un cliente sin forma de pago cargada no mostraba NADA —
    y un campo que no está no se distingue de uno que no existe.

    El "—" es información: dice "esto está sin cargar, cargalo en Editar
    cliente". Importa de verdad — el 03/08, de 3.973 clientes había UNO solo con
    el mail cargado.

    Cómo se comprueba que ningún campo está ENVUELTO en un `{% if %}`: hasta el
    rótulo, los `{% if %}` abiertos y los `{% endif %}` tienen que estar
    balanceados. Si quedó uno abierto, ese campo vive adentro y desaparece.
    """
    grilla = _grilla()
    for campo in CAMPOS:
        antes = grilla[:grilla.index(f'>{campo}</span>')]
        assert antes.count("{% if") == antes.count("{% endif %}"), (
            f"«{campo}» quedó dentro de un {{% if %}}: sin dato desaparece el "
            f"rótulo y nadie se entera de que falta cargarlo"
        )
    # Descuento se imprime siempre (0,0 %); los otros nueve caen en "—".
    assert grilla.count("ec-vacio") >= 7, "faltan los «—» de los campos sin dato"


def test_telefono_y_mail_van_pegados():
    """Dueña 04/08: *"mail y teléfono deberían estar más juntos"*, y *"buscá
    algún demo que ya tenga estas informaciones de clientes y copiate"*.

    El demo es `clientes/contactos.html` — el Directorio de contactos — donde
    Teléfono y Email son columnas vecinas, en ese orden. Se leen juntos porque
    se usan juntos: para contactar al cliente se mira uno o el otro. Acá son las
    dos primeras líneas de la segunda columna.
    """
    grilla = _grilla()
    i_tel = grilla.index('>Tel:</span>')
    i_mail = grilla.index('>Mail:</span>')
    assert i_tel < i_mail, "el mail va debajo del teléfono, como en el Directorio"
    # Sin ningún otro campo en el medio.
    assert not any(f'>{c}</span>' in grilla[i_tel:i_mail] for c in CAMPOS
                   if c not in ("Tel:", "Mail:")), (
        "se metió un campo entre el teléfono y el mail: vuelven a quedar lejos"
    )
    # Y el Directorio de contactos —la pantalla de la que se copió— sigue ahí.
    directorio = (_TPL_DIR.parents[2] / "clientes" / "templates" / "clientes"
                  / "contactos.html").read_text(encoding="utf-8")
    i_th_tel = directorio.index(">Teléfono<")
    assert "Email" in directorio[i_th_tel:i_th_tel + 400], (
        "cambió el Directorio: si allá el tel y el mail se separan, revisá si "
        "esta ficha todavía tiene sentido copiándolo"
    )


def test_el_codigo_de_cliente_se_ve():
    """Dueña 04/08: *"me gustaría que el código de cliente se vea más"*. Era un
    chip gris de 12,8 px; ahora es una placa oscura ANTES del nombre y del
    tamaño del nombre — el código es como ella nombra a los clientes."""
    ficha = _ficha()
    i_cod = ficha.index('class="ec-cod"')
    i_nom = ficha.index('class="ec-nombre"')
    assert i_cod < i_nom, "el código va ANTES del nombre: se lee primero"
    css = HTML[HTML.index(".ec-cod {"):HTML.index("}", HTML.index(".ec-cod {"))]
    tam = float(re.search(r"font-size: ([\d.]+)rem", css).group(1))
    assert tam >= 1.2, f"el código volvió a ser chico ({tam}rem)"


def test_el_stop_se_ve_bloqueado_y_ok():
    """Antes el chip sólo aparecía cuando estaba bloqueado. Como campo se
    muestran los dos estados: si "Stop" no dijera nada estando OK, volvería la
    duda de si el dato falta o el cliente está bien."""
    grilla = _grilla()
    bloque = grilla[grilla.index('>Stop:</span>'):]
    assert "Bloqueado" in bloque and ">OK<" in bloque
    assert "data.cliente.stop == 'S'" in bloque


def test_el_buscador_de_otro_cliente_sigue_entero():
    """Estaba arriba de todo con su propio label; ahora vive en la ficha. El id
    y el datalist no cambian: el `onsubmit` los busca por id."""
    ficha = _ficha()
    assert 'id="ec-nuevo"' in ficha
    assert 'list="ec-clientes-dl"' in ficha and 'id="ec-clientes-dl"' in ficha
    assert "informes.estado_cuenta" in ficha and "XCODEX" in ficha


# ─────────────────────────── permisos y jerarquía ───────────────────────────


def test_totalizar_y_editar_siguen_gateados_por_el_mismo_permiso():
    """El estado de cuenta lo ve todo el mundo. Totalizar ESCRIBE (re-liquida
    FIFO) y Editar cliente también: mover un botón de lugar no puede
    destaparlo para quien no lo tenía."""
    ficha = _ficha()
    i_tot = ficha.index("Totalizar (re-liquidar FIFO)")
    assert "tiene_permiso('clientes.ver')" in ficha[:i_tot]
    assert "data.facturas and" in ficha[:i_tot], (
        "Totalizar sin facturas no tiene nada que re-liquidar"
    )
    i_ed = ficha.index(">Editar cliente<")
    assert "tiene_permiso('clientes.editar')" in ficha[:i_ed]


def test_editar_cliente_va_despues_de_totalizar():
    """Dueña 04/08: *"editar cliente debería ir después de totalizar"*.

    Las dos son las acciones que ESCRIBEN, así que van juntas en la punta
    derecha de la barra, lejos de imprimir y de WhatsApp. Entre ellas manda la
    frecuencia: Totalizar se usa seguido, "Editar cliente" casi nunca.
    """
    ficha = _ficha()
    assert ficha.index("Totalizar (re-liquidar FIFO)") < ficha.index(">Editar cliente<")
    # Y las dos DESPUÉS del separador que las empuja a la derecha.
    assert ficha.index('class="ec-sep"') < ficha.index("Totalizar (re-liquidar FIFO)")


def test_totalizar_se_distingue_de_imprimir():
    """Antes los seis botones eran outline gris del mismo peso: la única acción
    que CAMBIA datos se veía igual que "imprimir". Queda en ámbar y al final."""
    assert "ec-btn-amb" in _ficha()


def test_whatsapp_recibe_su_clase_y_su_telefono():
    ficha = _ficha()
    i = ficha.index("_ec_boton_whatsapp.html")
    contexto = ficha[ficha.rfind("{% with", 0, i):i]
    assert "wa_clase = 'ec-btn ec-btn-wa'" in contexto
    assert "telefono | wa_tel" in contexto, "el filtro que normaliza el número"
    assert "estado_cuenta_pdf_cliente" in contexto


# ─────────────────────────── la parte de "elegante" ───────────────────────────


def test_la_ficha_no_se_imprime():
    """En papel manda el `.print-header` de dos líneas (dueña, 07/07: "deja
    SOLO nombre+código y el saldo"). Si la ficha perdiera el `no-print`, la
    hoja volvería a salir con los tiles y los botones."""
    assert '<div class="ec-ficha no-print">' in HTML


def test_el_css_de_la_ficha_no_depende_de_clases_tailwind_nuevas():
    """GOTCHA del repo: `static/tailwind.css` es un build JIT CONGELADO — una
    clase Tailwind que no esté ya compilada NO renderiza, y el bug se ve como
    "la pantalla salió sin estilo". Por eso la ficha trae su propio CSS."""
    ficha = _ficha()
    assert ".ec-ficha {" in HTML, "el CSS propio de la ficha desapareció"
    # Las clases del markup son `ec-*` o utilidades ya usadas en otras pantallas.
    assert "flex items-start justify-between mb-6" not in ficha


def test_no_quedo_la_tira_de_una_sola_pestania():
    """Un tab bar con UNA pestaña no es navegación, es una línea de ruido: la
    segunda pestaña ("Cuenta corriente") se sacó el 09/07 y la tira quedó."""
    assert 'border-b-2 border-sky-600 text-sky-700' not in HTML
