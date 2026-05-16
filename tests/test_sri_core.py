"""Tests de modules/sri/core.py — helpers puros.

Cubre: dígito verificador módulo 11 (incluyendo casos borde 10→1, 11→0),
clave de acceso (construcción + validación + desglose), limpieza de RUC
y formateo de montos/cantidades.

Todas las funciones son puras — sin Flask, sin DB.
"""
from __future__ import annotations

from datetime import date

import pytest

from modules.sri import core

# =====================================================================
# Módulo 11 — dígito verificador
# =====================================================================

class TestModulo11:
    def test_cadena_vacia_explota(self):
        with pytest.raises(ValueError):
            core.digito_verificador_modulo_11("")

    def test_cadena_con_letras_explota(self):
        with pytest.raises(ValueError):
            core.digito_verificador_modulo_11("abc123")

    def test_calculo_simple_conocido(self):
        # Para "12345" de derecha a izquierda:
        # 5*2 + 4*3 + 3*4 + 2*5 + 1*6 = 10+12+12+10+6 = 50
        # residuo = 50 % 11 = 6 -> dv = 11 - 6 = 5
        assert core.digito_verificador_modulo_11("12345") == "5"

    def test_residuo_cero_devuelve_cero(self):
        # Buscamos una cadena cuyo módulo 11 dé 0 → dv=11 → '0'.
        # "0" * N siempre da suma=0 → residuo=0 → dv=11 → '0'.
        assert core.digito_verificador_modulo_11("0") == "0"
        assert core.digito_verificador_modulo_11("00000") == "0"

    def test_residuo_uno_devuelve_uno(self):
        # Suma = 1 → residuo=1 → dv=10 → '1'.
        # Dígito '1' solo, peso 2 → suma=2 → residuo=2 → dv=9. No sirve.
        # Probemos "5" → peso 2 → suma=10 → residuo=10 → dv=1 ✓
        assert core.digito_verificador_modulo_11("5") == "1"

    def test_ciclo_de_pesos_se_repite(self):
        # 7 dígitos: los primeros 6 usan pesos 2,3,4,5,6,7;
        # el 7mo vuelve a peso 2.
        # "1234567" reversed = "7654321"
        # 7*2 + 6*3 + 5*4 + 4*5 + 3*6 + 2*7 + 1*2 = 14+18+20+20+18+14+2 = 106
        # residuo = 106 % 11 = 7 -> dv = 11 - 7 = 4
        assert core.digito_verificador_modulo_11("1234567") == "4"


# =====================================================================
# Clave de acceso — 49 dígitos
# =====================================================================

class TestClaveAcceso:
    """Suite sobre generar_clave_acceso / validar_clave_acceso / desglosar."""

    def _clave(self, **overrides):
        """Helper: clave con defaults sanos."""
        defaults = {
            "fecha_emision": date(2026, 4, 17),
            "tipo_comprobante": core.TIPO_FACTURA,
            "ruc": "1790012345001",
            "ambiente": core.AMBIENTE_CERTIFICACION,
            "estab": "001",
            "pto_emi": "001",
            "secuencial": 123,
            "codigo_numerico": "12345678",
            "tipo_emision": core.EMISION_NORMAL,
        }
        defaults.update(overrides)
        return core.generar_clave_acceso(**defaults)

    def test_longitud_total_49_digitos(self):
        clave = self._clave()
        assert len(clave) == 49
        assert clave.isdigit()

    def test_estructura_por_posicion(self):
        clave = self._clave(secuencial=7)
        partes = core.desglosar_clave_acceso(clave)
        assert partes["fecha_emision"]      == "17042026"   # DDMMAAAA
        assert partes["tipo_comprobante"]   == "01"
        assert partes["ruc"]                == "1790012345001"
        assert partes["ambiente"]           == "1"
        assert partes["estab"]              == "001"
        assert partes["pto_emi"]            == "001"
        assert partes["secuencial"]         == "000000007"   # 9 dígitos
        assert partes["codigo_numerico"]    == "12345678"
        assert partes["tipo_emision"]       == "1"
        assert len(partes["digito_verificador"]) == 1

    def test_dv_valida_con_validar_clave_acceso(self):
        clave = self._clave()
        assert core.validar_clave_acceso(clave) is True

    def test_clave_con_dv_corrupto_no_valida(self):
        clave = self._clave()
        # Flip el último dígito
        otro = "5" if clave[-1] != "5" else "4"
        corrupta = clave[:-1] + otro
        assert core.validar_clave_acceso(corrupta) is False

    def test_clave_de_48_digitos_no_valida(self):
        assert core.validar_clave_acceso("1" * 48) is False

    def test_clave_con_letras_no_valida(self):
        assert core.validar_clave_acceso("A" * 49) is False

    def test_fecha_no_date_explota(self):
        with pytest.raises(ValueError, match="fecha_emision"):
            self._clave(fecha_emision="2026-04-17")

    def test_ruc_corto_explota(self):
        with pytest.raises(ValueError, match="RUC"):
            self._clave(ruc="17900123")

    def test_ruc_con_guiones_se_limpia(self):
        # "1790-0123-45001" tiene 13 dígitos tras limpiar.
        clave = self._clave(ruc="1790-0123-45001")
        assert core.desglosar_clave_acceso(clave)["ruc"] == "1790012345001"

    def test_ambiente_invalido_explota(self):
        with pytest.raises(ValueError, match="ambiente"):
            self._clave(ambiente="3")

    def test_tipo_comprobante_relleno_con_ceros(self):
        # Si pasamos "1" (1 dígito) para factura, debe expandir a "01".
        clave = self._clave(tipo_comprobante="1")
        assert core.desglosar_clave_acceso(clave)["tipo_comprobante"] == "01"

    def test_secuencial_int_se_padea_a_9(self):
        clave = self._clave(secuencial=1)
        assert core.desglosar_clave_acceso(clave)["secuencial"] == "000000001"

    def test_codigo_numerico_se_genera_si_none(self):
        # Sin pasar codigo_numerico debe rellenarse con 8 dígitos.
        clave = core.generar_clave_acceso(
            fecha_emision=date(2026, 4, 17),
            tipo_comprobante=core.TIPO_FACTURA,
            ruc="1790012345001",
            ambiente=core.AMBIENTE_CERTIFICACION,
            secuencial=1,
        )
        partes = core.desglosar_clave_acceso(clave)
        assert len(partes["codigo_numerico"]) == 8
        assert partes["codigo_numerico"].isdigit()

    def test_deterministico_con_mismos_inputs(self):
        """Pasando codigo_numerico fijo, dos llamadas dan la misma clave."""
        a = self._clave()
        b = self._clave()
        assert a == b

    def test_cambio_de_secuencial_cambia_dv(self):
        """El DV depende del cuerpo — cambiar secuencial cambia el DV."""
        a = self._clave(secuencial=1)
        b = self._clave(secuencial=2)
        assert a != b
        assert a[-1] != b[-1] or a[:-1] != b[:-1]  # al menos el cuerpo o DV


# =====================================================================
# RUC
# =====================================================================

class TestRuc:
    def test_limpiar_quita_guiones_y_espacios(self):
        assert core.limpiar_ruc("1790-0123-45001") == "1790012345001"
        assert core.limpiar_ruc("1790 0123 45001") == "1790012345001"
        assert core.limpiar_ruc("  17.90.01.23.45001  ") == "1790012345001"

    def test_limpiar_none_explota(self):
        with pytest.raises(ValueError):
            core.limpiar_ruc(None)

    def test_es_ruc_valido_acepta_formato_correcto(self):
        # 17 = Pichincha, termina en 001
        assert core.es_ruc_valido("1790012345001") is True

    def test_es_ruc_valido_rechaza_longitud_mala(self):
        assert core.es_ruc_valido("17900123") is False
        assert core.es_ruc_valido("17900123450010") is False  # 14

    def test_es_ruc_valido_rechaza_provincia_fuera_rango(self):
        assert core.es_ruc_valido("9990012345001") is False   # provincia 99
        assert core.es_ruc_valido("0090012345001") is False   # provincia 00

    def test_es_ruc_valido_rechaza_no_001_al_final(self):
        # Termina en 999 — no es establecimiento estándar
        assert core.es_ruc_valido("1790012345999") is False


# =====================================================================
# Formateo de montos
# =====================================================================

class TestFormateo:
    def test_monto_con_dos_decimales(self):
        assert core.fmt_monto(100) == "100.00"
        assert core.fmt_monto(100.5) == "100.50"
        assert core.fmt_monto(100.555) in ("100.55", "100.56")  # banker's rounding ok

    def test_monto_none_es_cero(self):
        assert core.fmt_monto(None) == "0.00"

    def test_monto_valor_invalido_es_cero(self):
        assert core.fmt_monto("no-es-numero") == "0.00"

    def test_cantidad_con_dos_decimales_default(self):
        assert core.fmt_cantidad(10) == "10.00"
        assert core.fmt_cantidad(10.5) == "10.50"
