"""El navegador PRENDIDO y la caché de hojas — que mandar una hoja no tarde.

TMT 2026-08-26 (dueña): *"podemos hacer más rápido lo de mandar imagen, pdf y
whatsapp desde vendedor. tarda mucho tiempo"*.

De los 3,5-5,2 s medidos en producción para `/mi-cartera/cliente/<cod>/pdf`,
170 ms eran los datos y el HTML: todo lo demás era levantar y matar un
navegador por archivo. Estos tests protegen las tres cosas que se hicieron
para sacar esa espera, y sobre todo la que NO puede romperse:

  1. La caché no puede servir una hoja VIEJA. La clave es el hash del HTML: si
     entró un cheque, el HTML cambia y se dibuja de nuevo.
  2. El navegador prendido no puede hacer esperar a nadie: si no está listo, la
     hoja sale por el camino de siempre EN EL ACTO.
  3. Si el navegador prendido falla en el medio, la hoja SALE IGUAL.
"""
from __future__ import annotations

import base64
import json
import struct

import pytest

from modules._lib import cache_hojas, imagen_motor, navegador, pdf_motor

# ---------------------------------------------------------------------------
# La caché: rápida, pero incapaz de mandar un saldo viejo
# ---------------------------------------------------------------------------


def test_la_misma_hoja_no_se_dibuja_dos_veces():
    k = cache_hojas.clave("pdf", "<html>hola</html>")
    assert cache_hojas.obtener(k) is None
    cache_hojas.guardar(k, b"%PDF-1.4 uno")
    assert cache_hojas.obtener(k) == b"%PDF-1.4 uno"


def test_si_cambia_UNA_letra_del_html_es_otra_hoja():
    """El seguro entero del módulo: la clave es el HTML, no el cliente. Un
    cheque nuevo cambia el HTML, cambia el hash y se dibuja de nuevo — nunca se
    manda el saldo de hace cinco minutos."""
    a = cache_hojas.clave("pdf", "<p>Saldo 1.000</p>")
    b = cache_hojas.clave("pdf", "<p>Saldo 1.001</p>")
    assert a != b
    cache_hojas.guardar(a, b"vieja")
    assert cache_hojas.obtener(b) is None


def test_el_formato_tambien_es_parte_de_la_clave():
    """La misma hoja como PDF y como foto son dos archivos distintos."""
    assert cache_hojas.clave("pdf", "<p>x</p>") != cache_hojas.clave("png", "<p>x</p>")


def test_una_hoja_vencida_no_se_sirve(monkeypatch):
    monkeypatch.setattr(cache_hojas, "TTL_S", -1.0)
    k = cache_hojas.clave("pdf", "<p>vieja</p>")
    cache_hojas.guardar(k, b"%PDF vieja")
    assert cache_hojas.obtener(k) is None


def test_la_cache_tiene_techo_de_memoria(monkeypatch):
    """El servidor es el mismo que atiende la app: esto tiene que tener un
    techo y no una promesa."""
    monkeypatch.setattr(cache_hojas, "MAX_BYTES", 300)
    for i in range(6):
        cache_hojas.guardar(cache_hojas.clave("pdf", i), b"x" * 100)
    assert cache_hojas.estado()["bytes"] <= 300
    # La última siempre queda: es la que alguien acaba de pedir.
    assert cache_hojas.obtener(cache_hojas.clave("pdf", 5)) == b"x" * 100


def test_una_hoja_mas_grande_que_el_techo_no_se_guarda(monkeypatch):
    monkeypatch.setattr(cache_hojas, "MAX_BYTES", 10)
    k = cache_hojas.clave("pdf", "gorda")
    cache_hojas.guardar(k, b"x" * 100)
    assert cache_hojas.obtener(k) is None
    cache_hojas.guardar(k, b"")          # ni una hoja vacía
    assert cache_hojas.obtener(k) is None


def test_guardar_dos_veces_la_misma_clave_no_suma_peso():
    k = cache_hojas.clave("pdf", "repetida")
    cache_hojas.guardar(k, b"x" * 50)
    cache_hojas.guardar(k, b"y" * 50)
    assert cache_hojas.estado() == {"hojas": 1, "bytes": 50}
    assert cache_hojas.obtener(k) == b"y" * 50


def test_las_vencidas_se_tiran_cuando_entra_una_nueva(monkeypatch):
    monkeypatch.setattr(cache_hojas, "TTL_S", -1.0)
    cache_hojas.guardar(cache_hojas.clave("pdf", "vieja"), b"x" * 10)
    monkeypatch.setattr(cache_hojas, "TTL_S", 300.0)
    cache_hojas.guardar(cache_hojas.clave("pdf", "nueva"), b"y" * 10)
    assert cache_hojas.estado()["hojas"] == 1


def test_limpiar_la_deja_en_cero():
    cache_hojas.guardar(cache_hojas.clave("pdf", "a"), b"x")
    cache_hojas.limpiar()
    assert cache_hojas.estado() == {"hojas": 0, "bytes": 0}


# ---------------------------------------------------------------------------
# El websocket escrito a mano — el pedazo que no se puede probar "mirándolo"
# ---------------------------------------------------------------------------


def _trama(cuerpo: bytes, op: int = 0x1, fin: bool = True) -> bytes:
    """Una trama del SERVIDOR (sin máscara), como las que manda Chrome."""
    b0 = (0x80 if fin else 0) | op
    n = len(cuerpo)
    if n < 126:
        return struct.pack("!BB", b0, n) + cuerpo
    if n < 65536:
        return struct.pack("!BBH", b0, 126, n) + cuerpo
    return struct.pack("!BBQ", b0, 127, n) + cuerpo


class _SocketFalso:
    """Un socket de mentira: devuelve lo que se le puso y anota lo que se envió."""

    def __init__(self, *trozos: bytes):
        self.cola = list(trozos)
        self.enviado: list[bytes] = []
        self.cerrado = False

    def settimeout(self, t):
        pass

    def sendall(self, datos):
        self.enviado.append(datos)

    def recv(self, n):
        return self.cola.pop(0) if self.cola else b""

    def close(self):
        self.cerrado = True


def _ws_con(monkeypatch, *trozos, handshake=b"HTTP/1.1 101 Switching Protocols\r\n\r\n"):
    falso = _SocketFalso(handshake, *trozos)
    monkeypatch.setattr(navegador.socket, "create_connection",
                        lambda *a, **k: falso)
    return navegador._Ws("127.0.0.1", 1, "/x", 5.0), falso


def test_el_websocket_lee_un_mensaje(monkeypatch):
    ws, _ = _ws_con(monkeypatch, _trama(b'{"id":1}'))
    assert ws.recibir() == '{"id":1}'


def test_el_websocket_junta_las_tramas_partidas(monkeypatch):
    """Una foto de 300 kB llega en pedazos: leer sólo el primero deja el JSON
    cortado por la mitad."""
    ws, _ = _ws_con(monkeypatch,
                    _trama(b'{"id":', fin=False), _trama(b'1}', op=0x0))
    assert ws.recibir() == '{"id":1}'


@pytest.mark.parametrize("largo", [200, 70000])
def test_el_websocket_lee_mensajes_largos(monkeypatch, largo):
    cuerpo = b"a" * largo
    ws, _ = _ws_con(monkeypatch, _trama(cuerpo))
    assert ws.recibir() == cuerpo.decode()


def test_el_websocket_ignora_los_pings(monkeypatch):
    ws, _ = _ws_con(monkeypatch, _trama(b"", op=0x9), _trama(b"hola"))
    assert ws.recibir() == "hola"


def test_si_el_navegador_cierra_el_websocket_se_avisa(monkeypatch):
    ws, _ = _ws_con(monkeypatch, _trama(b"", op=0x8))
    with pytest.raises(OSError):
        ws.recibir()


def test_si_el_navegador_corta_en_el_medio_se_avisa(monkeypatch):
    ws, _ = _ws_con(monkeypatch, _trama(b"hola")[:3])
    with pytest.raises(OSError):
        ws.recibir()


def test_un_handshake_rechazado_no_pasa_por_bueno(monkeypatch):
    with pytest.raises(OSError):
        _ws_con(monkeypatch, handshake=b"HTTP/1.1 500 Server Error\r\n\r\n")


def test_un_handshake_cortado_no_cuelga(monkeypatch):
    with pytest.raises(OSError):
        _ws_con(monkeypatch, handshake=b"")


@pytest.mark.parametrize("largo", [10, 300, 70000])
def test_lo_que_se_envia_va_enmascarado(monkeypatch, largo):
    """El cliente SIEMPRE enmascara (RFC 6455): sin eso Chrome corta la
    conexión y no dice por qué."""
    ws, falso = _ws_con(monkeypatch)
    ws.enviar("x" * largo)
    trama = falso.enviado[-1]
    assert trama[0] == 0x81
    assert trama[1] & 0x80, "sin el bit de máscara"
    salto = 2 + (0 if largo < 126 else 2 if largo < 65536 else 8)
    mascara, cuerpo = trama[salto:salto + 4], trama[salto + 4:]
    limpio = bytes(b ^ mascara[i % 4] for i, b in enumerate(cuerpo))
    assert limpio == b"x" * largo


def test_cerrar_el_websocket_cierra_el_socket(monkeypatch):
    ws, falso = _ws_con(monkeypatch)
    ws.cerrar()
    assert falso.cerrado


# ---------------------------------------------------------------------------
# La conversación con el navegador
# ---------------------------------------------------------------------------


class _WsCdp:
    """Un navegador de mentira que contesta CDP.

    `respuestas` mapea método → resultado. Antes de cada respuesta mete un
    EVENTO, que es lo que manda Chrome todo el tiempo y lo que el lector tiene
    que saber saltear.
    """

    def __init__(self, respuestas: dict, romper: str | None = None):
        self.respuestas = respuestas
        self.romper = romper
        self.pedidos: list[dict] = []
        self.cola: list[str] = []

    def enviar(self, texto: str) -> None:
        msg = json.loads(texto)
        self.pedidos.append(msg)
        self.cola.append(json.dumps(
            {"method": "Page.frameNavigated", "params": {}}))
        if msg["method"] == self.romper:
            self.cola.append(json.dumps(
                {"id": msg["id"], "error": {"message": "no puedo"}}))
            return
        self.cola.append(json.dumps(
            {"id": msg["id"],
             "result": self.respuestas.get(msg["method"], {})}))

    def recibir(self) -> str:
        return self.cola.pop(0)

    def cerrar(self) -> None:
        pass


_RESPUESTAS = {
    "Target.createTarget": {"targetId": "T1"},
    "Target.attachToTarget": {"sessionId": "S1"},
    "Runtime.evaluate": {"result": {"value": "complete|file:///hoja.html"}},
    "Page.printToPDF": {"data": base64.b64encode(b"%PDF-1.4 rapido").decode()},
    "Page.captureScreenshot": {"data": base64.b64encode(b"\x89PNG rapido").decode()},
}


def _nav_falso(respuestas=None, romper=None):
    nav = navegador._Navegador()
    nav.ws = _WsCdp(dict(respuestas or _RESPUESTAS), romper=romper)
    return nav


def test_una_pestana_imprime_el_pdf_y_se_cierra(tmp_path):
    nav = _nav_falso()
    datos = nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")
    assert datos == b"%PDF-1.4 rapido"
    metodos = [p["method"] for p in nav.ws.pedidos]
    assert "Page.printToPDF" in metodos
    assert metodos[-1] == "Target.closeTarget", "la pestaña quedó abierta"


def test_la_foto_pide_la_ventana_del_tamano_de_la_hoja(tmp_path):
    """`captureScreenshot` saca EXACTAMENTE la ventana: si no se le fija el
    tamaño, la foto sale de 800x600 y le falta media cartera."""
    nav = _nav_falso()
    datos = nav.hoja("<html></html>", tmp_path, medidas=(900, 1400), formato="png")
    assert datos == b"\x89PNG rapido"
    medidas = [p for p in nav.ws.pedidos
               if p["method"] == "Emulation.setDeviceMetricsOverride"]
    assert medidas and medidas[0]["params"]["width"] == 900
    assert medidas[0]["params"]["height"] == 1400
    assert any(p["method"] == "Emulation.setScrollbarsHidden"
               for p in nav.ws.pedidos), "la barra de scroll se dibuja encima"


def test_la_foto_se_achica_al_alto_REAL_del_contenido(tmp_path):
    """TMT 2026-08-27: la ventana se abre con el alto estimado (que estima
    para arriba a propósito), pero antes de la foto se le pregunta a la
    página cuánto mide y se achica la ventana al contenido más un margen.
    Medido en producción: 953 px de contenido en una ventana de 1988."""
    class _Ws(_WsCdp):
        def enviar(self, texto):
            msg = json.loads(texto)
            if (msg["method"] == "Runtime.evaluate"
                    and "document.body" in msg["params"]["expression"]):
                self.respuestas["Runtime.evaluate"] = {"result": {"value": "953"}}
            else:
                self.respuestas["Runtime.evaluate"] = {
                    "result": {"value": "complete|file:///hoja.html"}}
            super().enviar(texto)

    nav = navegador._Navegador()
    nav.ws = _Ws(dict(_RESPUESTAS))
    datos = nav.hoja("<html></html>", tmp_path, medidas=(900, 1988), formato="png")
    assert datos == b"\x89PNG rapido"
    medidas = [p["params"] for p in nav.ws.pedidos
               if p["method"] == "Emulation.setDeviceMetricsOverride"]
    assert len(medidas) == 2, "no se reajusto la ventana al contenido"
    assert medidas[0]["height"] == 1988          # la estimada, para cargar
    assert medidas[1]["height"] == 953 + 60      # la real, para la foto
    assert medidas[1]["width"] == 900


def test_si_la_pagina_no_contesta_su_alto_la_foto_sale_igual(tmp_path):
    """El fake contesta el readyState para TODAS las evaluaciones: el alto no
    se puede leer (no es un numero) y la foto tiene que salir con la ventana
    estimada, sin segundo override y sin error — como hasta hoy."""
    nav = _nav_falso()
    datos = nav.hoja("<html></html>", tmp_path, medidas=(900, 1400), formato="png")
    assert datos == b"\x89PNG rapido"
    medidas = [p["params"] for p in nav.ws.pedidos
               if p["method"] == "Emulation.setDeviceMetricsOverride"]
    assert len(medidas) == 1
    assert medidas[0]["height"] == 1400


def test_el_alto_medido_no_pasa_del_techo_ni_del_piso(tmp_path):
    """Una pagina que mide 30000 px no puede pedir un bitmap de 30000
    (memoria del servidor); una de 10 px no puede pedir una ventana de 70."""
    class _Ws(_WsCdp):
        def __init__(self, respuestas, alto):
            super().__init__(respuestas)
            self.alto = alto

        def enviar(self, texto):
            msg = json.loads(texto)
            if (msg["method"] == "Runtime.evaluate"
                    and "document.body" in msg["params"]["expression"]):
                self.respuestas["Runtime.evaluate"] = {"result": {"value": self.alto}}
            else:
                self.respuestas["Runtime.evaluate"] = {
                    "result": {"value": "complete|file:///hoja.html"}}
            super().enviar(texto)

    for alto, esperado in (("30000", 20000), ("10", 300)):
        nav = navegador._Navegador()
        nav.ws = _Ws(dict(_RESPUESTAS), alto)
        nav.hoja("<html></html>", tmp_path, medidas=(900, 1988), formato="png")
        medidas = [p["params"] for p in nav.ws.pedidos
                   if p["method"] == "Emulation.setDeviceMetricsOverride"]
        assert medidas[-1]["height"] == esperado


def test_el_pdf_sale_sin_encabezado_del_navegador(tmp_path):
    """Nada de "1/2" ni la URL del archivo temporal en la hoja que ve el
    cliente — es lo que hace `--no-pdf-header-footer` en el otro camino."""
    nav = _nav_falso()
    nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")
    params = next(p["params"] for p in nav.ws.pedidos
                  if p["method"] == "Page.printToPDF")
    assert params["displayHeaderFooter"] is False
    assert params["printBackground"] is False
    assert params["preferCSSPageSize"] is True


def test_se_espera_a_que_la_hoja_TERMINE_de_cargar(tmp_path):
    """Se pregunta `readyState` en vez de escuchar el evento de carga: el del
    `about:blank` con el que nace la pestaña puede llegar tarde y ahí se
    fotografía una página en blanco."""
    respuestas = dict(_RESPUESTAS)
    vueltas = []

    class _Ws(_WsCdp):
        def enviar(self, texto):
            msg = json.loads(texto)
            if msg["method"] == "Runtime.evaluate":
                vueltas.append(1)
                valor = ("complete|file:///hoja.html" if len(vueltas) > 2
                         else "loading|about:blank")
                self.respuestas["Runtime.evaluate"] = {"result": {"value": valor}}
            super().enviar(texto)

    nav = navegador._Navegador()
    nav.ws = _Ws(respuestas)
    nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")
    assert len(vueltas) == 3, "no esperó a que la hoja estuviera entera"


def test_si_la_hoja_no_carga_nunca_no_se_cuelga_para_siempre(tmp_path, monkeypatch):
    monkeypatch.setattr(navegador, "DOC_S", 0.2)
    respuestas = dict(_RESPUESTAS,
                      **{"Runtime.evaluate": {"result": {"value": "loading|about:blank"}}})
    nav = _nav_falso(respuestas)
    with pytest.raises(OSError):
        nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")


def test_un_archivo_vacio_no_pasa_por_bueno(tmp_path):
    respuestas = dict(_RESPUESTAS, **{"Page.printToPDF": {"data": ""}})
    nav = _nav_falso(respuestas)
    with pytest.raises(OSError):
        nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")


def test_un_error_del_navegador_se_cuenta_como_error(tmp_path):
    nav = _nav_falso(romper="Page.printToPDF")
    with pytest.raises(OSError):
        nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")


def test_los_eventos_de_la_pestana_no_se_confunden_con_la_respuesta(tmp_path):
    """Chrome manda eventos todo el tiempo; el que espera el resultado de un
    comando tiene que saltearlos y no quedarse con el primero que pasa."""
    nav = _nav_falso()
    nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")
    assert nav.ws.cola == [], "quedaron mensajes sin leer"


# ---------------------------------------------------------------------------
# La regla de oro: esto NO puede hacer esperar a nadie
# ---------------------------------------------------------------------------


def test_si_el_navegador_no_esta_prendido_se_contesta_en_el_acto(monkeypatch):
    """`None` quiere decir "por acá no salió" y el que llamó sigue por el
    camino de siempre. NUNCA se levanta un navegador adentro de un request."""
    llamadas = []
    monkeypatch.setattr(navegador._NAV, "arrancar",
                        lambda: llamadas.append(1) or True)
    assert navegador.pdf("<html></html>", None) is None
    assert navegador.png("<html></html>", None, 900, 900) is None
    assert llamadas == [], "un request se puso a levantar el navegador"


def test_apagarlo_por_variable_de_entorno(monkeypatch):
    monkeypatch.setenv(navegador.VAR_APAGAR, "0")
    assert navegador.apagado() is True
    assert navegador.pdf("<html></html>", None) is None
    assert navegador._NAV.arrancar() is False
    assert navegador.arrancar_en_segundo_plano() is False


def test_prendido_de_fabrica(monkeypatch):
    monkeypatch.delenv(navegador.VAR_APAGAR, raising=False)
    assert navegador.apagado() is False


def test_despues_de_un_fallo_no_se_reintenta_en_la_hoja_siguiente(monkeypatch, tmp_path):
    """Cada intento fallido le cuesta al vendedor la espera de darse cuenta
    ANTES de caer al camino viejo. Después de uno, el navegador queda apagado
    un rato y las hojas salen como salían ayer."""
    monkeypatch.setattr(navegador._NAV, "vivo", lambda: True)
    monkeypatch.setattr(navegador._NAV, "matar", lambda: None)
    monkeypatch.setattr(navegador._NAV, "proximo_intento", 0.0)

    def _explota(*a, **k):
        raise OSError("se murió")

    monkeypatch.setattr(navegador._NAV, "hoja", _explota)
    assert navegador.pdf("<html></html>", tmp_path) is None
    assert navegador._NAV.proximo_intento > 0, "se vuelve a intentar de una"


def test_una_hoja_tiene_UN_techo_y_no_uno_por_comando(monkeypatch, tmp_path):
    """Si cada comando esperara su propio techo, la suma no la miraría nadie y
    una pestaña colgada se llevaría puesto un worker durante minutos."""
    monkeypatch.setattr(navegador, "DOC_S", 0.2)
    respuestas = dict(_RESPUESTAS,
                      **{"Runtime.evaluate": {"result": {"value": "loading|about:blank"}}})
    nav = _nav_falso(respuestas)
    import time as _t
    a = _t.monotonic()
    with pytest.raises(OSError):
        nav.hoja("<html></html>", tmp_path, medidas=None, formato="pdf")
    assert _t.monotonic() - a < 2.0


def test_si_falla_en_el_medio_la_hoja_sale_igual(monkeypatch, tmp_path):
    """El navegador se murió con la hoja a medio imprimir: se lo tira, se
    devuelve `None` y el estado de cuenta sale por el camino de siempre. El
    vendedor no se entera de nada."""
    matado = []
    monkeypatch.setattr(navegador._NAV, "vivo", lambda: True)
    monkeypatch.setattr(navegador._NAV, "matar", lambda: matado.append(1))

    def _explota(*a, **k):
        raise OSError("se murió")

    monkeypatch.setattr(navegador._NAV, "hoja", _explota)
    assert navegador.pdf("<html></html>", tmp_path) is None
    assert matado == [1], "un navegador roto quedó dando vueltas"


def test_el_hilo_no_arranca_bajo_pytest():
    """Ningún test levanta un Chromium de verdad sin pedirlo."""
    assert navegador.arrancar_en_segundo_plano() is False


def test_sin_navegador_en_el_servidor_no_se_intenta_prender(monkeypatch):
    monkeypatch.setattr(navegador._NAV, "proximo_intento", 0.0)
    monkeypatch.setattr(pdf_motor, "binario", lambda: None)
    assert navegador._NAV.arrancar() is False


def test_un_arranque_fallido_no_se_reintenta_en_cada_hoja(monkeypatch):
    """Si en este servidor el modo CDP no anda, que no se note en nada más que
    un renglón del log: no se reintenta hasta dentro de un rato."""
    intentos = []
    monkeypatch.setattr(navegador._NAV, "proximo_intento", 0.0)
    monkeypatch.setattr(pdf_motor, "binario", lambda: "/bin/falso")

    def _no_levanta(exe):
        intentos.append(exe)
        raise OSError("no levantó")

    monkeypatch.setattr(navegador._NAV, "_levantar", _no_levanta)
    assert navegador._NAV.arrancar() is False
    assert navegador._NAV.arrancar() is False
    assert len(intentos) == 1, "reintentó de una"


def test_el_estado_lo_puede_mirar_el_health():
    est = navegador.estado()
    assert set(est) == {"prendido", "apagado_por_env", "segundos_sin_uso"}


# ---------------------------------------------------------------------------
# Los dos motores, enchufados a lo de arriba
# ---------------------------------------------------------------------------


def test_el_pdf_sale_del_navegador_prendido_si_lo_hay(monkeypatch):
    """Y sin levantar uno nuevo: el `subprocess` ni se toca."""
    monkeypatch.setattr(pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr("modules._lib.navegador.pdf",
                        lambda html, static: b"%PDF-1.4 prendido")
    monkeypatch.setattr(pdf_motor.subprocess, "run", _prohibido)
    assert pdf_motor.desde_html("<html>a</html>") == b"%PDF-1.4 prendido"


def test_el_pdf_ya_dibujado_no_se_vuelve_a_dibujar(monkeypatch):
    """El botón de WhatsApp pide la hoja al apoyar el dedo y otra vez al
    tocar: la segunda tiene que salir de memoria."""
    veces = []
    monkeypatch.setattr(pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr("modules._lib.navegador.pdf",
                        lambda html, static: veces.append(1) or b"%PDF-1.4 x")
    pdf_motor.desde_html("<html>a</html>")
    pdf_motor.desde_html("<html>a</html>")
    assert len(veces) == 1


def test_si_no_hay_navegador_prendido_el_pdf_sale_por_el_camino_viejo(monkeypatch):
    monkeypatch.setattr(pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr("modules._lib.navegador.pdf", lambda html, static: None)
    corridas = []

    def _correr(cmd, **kw):
        corridas.append(cmd)
        salida = [c for c in cmd if str(c).startswith("--print-to-pdf=")][0]
        from pathlib import Path
        Path(salida.split("=", 1)[1]).write_bytes(b"%PDF-1.4 viejo")

    monkeypatch.setattr(pdf_motor.subprocess, "run", _correr)
    assert pdf_motor.desde_html("<html>b</html>") == b"%PDF-1.4 viejo"
    assert corridas, "no cayó al camino de siempre"


def test_la_foto_sale_del_navegador_prendido_si_lo_hay(monkeypatch):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (900, 300), (255, 255, 255)).save(buf, format="PNG")
    monkeypatch.setattr(pdf_motor, "binario", lambda: "/bin/falso")
    monkeypatch.setattr("modules._lib.navegador.png",
                        lambda html, static, ancho, alto: buf.getvalue())
    monkeypatch.setattr(imagen_motor.subprocess, "run", _prohibido)
    assert imagen_motor.desde_html("<html>c</html>", filas=2)


def test_la_foto_ya_sacada_no_se_vuelve_a_sacar(monkeypatch):
    veces = []
    monkeypatch.setattr(imagen_motor.pdf_motor, "binario", lambda: "/bin/falso")

    def _falsa(exe, html, static, alto):
        veces.append(alto)
        return _png_blanco(alto)

    monkeypatch.setattr(imagen_motor, "_sacar_foto", _falsa)
    imagen_motor.desde_html("<html>d</html>", filas=2)
    imagen_motor.desde_html("<html>d</html>", filas=2)
    assert len(veces) == 1


def _png_blanco(alto: int) -> bytes:
    import io

    from PIL import Image

    im = Image.new("RGB", (100, alto), (255, 255, 255))
    for y in range(10, 40):
        for x in range(10, 40):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _prohibido(*a, **k):
    raise AssertionError("se levantó un navegador teniendo uno prendido")


# ---------------------------------------------------------------------------
# Los navegadores huérfanos — el deploy mata la app y NO se lleva a los hijos
# ---------------------------------------------------------------------------


@pytest.fixture
def temporal(monkeypatch, tmp_path):
    monkeypatch.setattr(navegador.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _carpeta_con(temporal, dueno: int, pid: int, exe: str = "/bin/chromium"):
    d = temporal / f"pc-nav-{dueno}"
    d.mkdir()
    (d / "navegador.pid").write_text(f"{pid}\n{exe}\n", encoding="utf-8")
    return d


def test_el_navegador_de_un_proceso_MUERTO_se_mata(temporal, monkeypatch):
    """El deploy para la app con `Stop-Process -Force` y eso NO se lleva a los
    hijos: sin este barrido cada deploy dejaría un headless prendido para
    siempre, en el mismo servidor que atiende la app."""
    muertos = []
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    monkeypatch.setattr(navegador, "_es_ese_navegador", lambda pid, exe: True)
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: muertos.append(pid))
    carpeta = _carpeta_con(temporal, 4242, 777)

    navegador._barrer_huerfanos()

    assert muertos == [777]
    assert not carpeta.exists(), "quedó la carpeta del perfil"


def test_el_navegador_del_proceso_de_al_lado_NO_se_toca(temporal, monkeypatch):
    """La oficina (5002) y el portal (5004) corren el mismo código en dos
    procesos. Barrer de más sería apagarle el navegador al otro."""
    muertos = []
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: True)
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: muertos.append(pid))
    carpeta = _carpeta_con(temporal, 4242, 777)

    navegador._barrer_huerfanos()

    assert muertos == []
    assert carpeta.exists()


def test_el_barrido_no_se_mata_a_si_mismo(temporal, monkeypatch):
    import os

    muertos = []
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: muertos.append(pid))
    carpeta = _carpeta_con(temporal, os.getpid(), 777)

    navegador._barrer_huerfanos()

    assert muertos == []
    assert carpeta.exists()


def test_una_carpeta_con_otro_nombre_no_lo_confunde(temporal, monkeypatch):
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    (temporal / "pc-nav-otracosa").mkdir()
    navegador._barrer_huerfanos()          # no tira
    assert (temporal / "pc-nav-otracosa").exists()


def test_no_se_mata_un_pid_que_ya_es_de_otro_programa(temporal, monkeypatch):
    """Los pid se reciclan. Antes de matar se confirma que en ese número está
    el mismo programa que anotamos, no el Excel de un contador."""
    muertos = []
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    monkeypatch.setattr(navegador, "_es_ese_navegador", lambda pid, exe: False)
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: muertos.append(pid))
    _carpeta_con(temporal, 4242, 777)

    navegador._barrer_huerfanos()

    assert muertos == []


def test_una_carpeta_sin_apunte_no_es_un_problema(temporal, monkeypatch):
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    (temporal / "pc-nav-4242").mkdir()
    navegador._barrer_huerfanos()          # calladito
    assert not (temporal / "pc-nav-4242").exists()


def test_un_apunte_ilegible_no_rompe_el_barrido(temporal, monkeypatch):
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    d = temporal / "pc-nav-4242"
    d.mkdir()
    (d / "navegador.pid").write_text("cualquier cosa", encoding="utf-8")
    navegador._barrer_huerfanos()
    assert not d.exists()


def test_si_matar_falla_el_barrido_sigue(temporal, monkeypatch):
    def _explota(pid):
        raise OSError("no tengo permiso")

    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    monkeypatch.setattr(navegador, "_es_ese_navegador", lambda pid, exe: True)
    monkeypatch.setattr(navegador, "_matar_pid", _explota)
    carpeta = _carpeta_con(temporal, 4242, 777)
    navegador._barrer_huerfanos()
    assert not carpeta.exists()


def test_en_posix_se_reconoce_al_navegador_por_su_linea_de_comando(monkeypatch):
    import os

    monkeypatch.setattr(navegador.sys, "platform", "linux")
    assert navegador._es_ese_navegador(os.getpid(), "/no/existe/chromium") is False


def test_en_windows_no_se_pregunta_con_os_kill(monkeypatch):
    """⚠ En Windows `os.kill(pid, 0)` NO pregunta: MATA. Por eso ahí se
    pregunta con `tasklist`, y este test es el que lo sostiene."""
    monkeypatch.setattr(navegador.sys, "platform", "win32")
    monkeypatch.setattr(navegador.os, "kill", _prohibido)
    monkeypatch.setattr(navegador, "_imagen_de", lambda pid: "msedge.exe")
    assert navegador._proceso_vivo(1234) is True
    monkeypatch.setattr(navegador, "_imagen_de", lambda pid: "")
    assert navegador._proceso_vivo(1234) is False


def test_en_windows_se_mata_con_taskkill(monkeypatch):
    corridas = []
    monkeypatch.setattr(navegador.sys, "platform", "win32")
    monkeypatch.setattr(navegador.subprocess, "run",
                        lambda cmd, **kw: corridas.append(cmd))
    navegador._matar_pid(4321)
    assert corridas[0][:2] == ["taskkill", "/PID"]
    assert "4321" in corridas[0]


class _Salida:
    def __init__(self, texto):
        self.stdout = texto


def test_la_imagen_de_un_pid_sale_de_tasklist(monkeypatch):
    monkeypatch.setattr(navegador.sys, "platform", "win32")
    monkeypatch.setattr(navegador.subprocess, "run",
                        lambda cmd, **kw: _Salida('"msedge.exe","4321","Services"'))
    assert navegador._imagen_de(4321) == "msedge.exe"
    assert navegador._es_ese_navegador(
        4321, r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") is True


def test_si_tasklist_no_contesta_no_se_mata_a_nadie(monkeypatch):
    monkeypatch.setattr(navegador.sys, "platform", "win32")
    monkeypatch.setattr(navegador.subprocess, "run",
                        lambda cmd, **kw: _Salida("INFO: no tasks are running"))
    assert navegador._imagen_de(4321) == ""

    def _explota(cmd, **kw):
        raise OSError("no está tasklist")

    monkeypatch.setattr(navegador.subprocess, "run", _explota)
    assert navegador._imagen_de(4321) == ""


def test_el_pid_del_navegador_queda_anotado(monkeypatch, temporal):
    """Sin el apunte, el próximo arranque no sabe a quién matar."""
    import os

    class _Proc:
        pid = 999
        returncode = 0

        def poll(self):
            return 0          # se "cierra solo": no interesa acá

    monkeypatch.setattr(navegador.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(navegador, "_barrer_huerfanos", lambda: None)
    with pytest.raises(OSError):
        navegador._NAV._levantar("/bin/chromium")
    apunte = (temporal / f"pc-nav-{os.getpid()}" / "navegador.pid").read_text()
    assert apunte.splitlines() == ["999", "/bin/chromium"]


# ---------------------------------------------------------------------------
# Contra un navegador DE VERDAD — lo único que prueba que el archivo es el mismo
# ---------------------------------------------------------------------------


@pytest.mark.skipif(pdf_motor.binario() is None,
                    reason="no hay navegador en esta máquina")
def test_el_archivo_del_navegador_prendido_es_EL_MISMO(tmp_path):
    """La pregunta que decide si esto se puede prender: ¿sale el mismo archivo?

    Se dibuja la misma hoja por los dos caminos y se comparan. La foto tiene
    que salir IDÉNTICA después del recorte — que es lo que sale del programa:
    `desde_html` recorta SIEMPRE, venga la foto del camino que venga. En crudo
    ya no pueden ser iguales a propósito: al navegador prendido se le pregunta
    cuánto mide el contenido y saca la foto con la ventana justa (TMT
    2026-08-27, "¿podemos mejorar?"), mientras que `--screenshot` es una
    corrida ciega con la ventana estimada. El recorte de ambas termina en el
    mismo cajón de píxeles, y ahí sí: byte por byte. El PDF cambia sólo en la
    hora que Skia le escribe adentro (`/CreationDate`), así que se compara el
    largo y el cuerpo salteando esa línea.

    Si algún día un Chrome nuevo cambia esto, este test se pone rojo ANTES de
    que un cliente reciba una hoja distinta.
    """
    html = ("<!doctype html><meta charset='utf-8'>"
            "<style>body{font:10pt system-ui;margin:12px}"
            "td{border:1px solid #999;padding:2px 6px}</style>"
            "<h1>Estado de cuenta</h1><table>"
            + "".join(f"<tr><td>{i}</td><td>26-08-2026</td></tr>" for i in range(25))
            + "</table>")
    estatico = tmp_path / "static"
    estatico.mkdir()

    navegador._NAV.proximo_intento = 0.0
    try:
        assert navegador._NAV.arrancar(), "no levantó el navegador"
        rapido_pdf = navegador.pdf(html, estatico)
        rapido_png = navegador.png(html, estatico, imagen_motor.ANCHO, 1200)
    finally:
        navegador._NAV.matar()

    viejo_pdf = pdf_motor.desde_html(html, static_dir=estatico)
    cache_hojas.limpiar()
    viejo_png = imagen_motor._sacar_foto(
        pdf_motor.binario(), html, estatico, 1200)

    assert (imagen_motor._recortar(rapido_png)[0]
            == imagen_motor._recortar(viejo_png)[0]), "la foto NO es la misma"
    assert len(rapido_pdf) == len(viejo_pdf), "el PDF cambió de tamaño"
    def sin_fecha(b: bytes) -> bytes:
        """El PDF sin la línea de la hora: es lo único que cambia entre los dos."""
        return b"".join(x for x in b.split(b"\n") if b"Date (D:" not in x)

    assert sin_fecha(rapido_pdf) == sin_fecha(viejo_pdf), "el PDF NO es el mismo"
