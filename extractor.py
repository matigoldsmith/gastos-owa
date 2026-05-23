"""
Extracción de datos de gastos desde fotos usando Mistral Pixtral-12B.
Motor: Mistral AI REST API — tier gratuito, costo $0.
Sin dependencia del SDK mistralai (usa requests directamente).
"""
import base64
import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

_MISTRAL_KEY   = os.getenv("MISTRAL_API_KEY")    # owa605 — principal
_MISTRAL_KEY_2 = os.getenv("MISTRAL_API_KEY_2")  # owa605.g66 — fallback
if not _MISTRAL_KEY:
    raise EnvironmentError("MISTRAL_API_KEY no encontrada en .env")

_API_URL      = "https://api.mistral.ai/v1/chat/completions"
_MODEL_VISION = "pixtral-12b-2409"     # Multimodal — fotos
_MODEL_TEXT   = "mistral-small-latest" # Solo texto — emails

# Cuenta activa: empieza en owa605, cambia a owa605.g66 si se agota
_active_key     = _MISTRAL_KEY
_active_account = "owa605"


def _get_headers() -> dict:
    return {"Authorization": f"Bearer {_active_key}", "Content-Type": "application/json"}


def _switch_to_fallback():
    global _active_key, _active_account
    if _MISTRAL_KEY_2 and _active_key != _MISTRAL_KEY_2:
        logger.warning("Cuota owa605 agotada — cambiando a owa605.g66")
        _active_key     = _MISTRAL_KEY_2
        _active_account = "owa605.g66"
        return True
    return False


def get_active_account() -> str:
    return _active_account

_PROMPT_FOTO = """\
Mira esta imagen con cuidado. ¿Es un comprobante de pago YA COMPLETADO en un comercio?

CONSIDERA RECIBO (es_gasto=true) SOLO si ves un documento fiscal o comprobante físico de pago a un comercio:
- Boleta de venta electrónica (texto "BOLETA", número de boleta, RUT del emisor)
- Factura electrónica o física de un comercio (con RUT/CNPJ/tax ID, folio, giro)
- Ticket de caja impreso de un comercio (supermercado, restaurante, tienda, farmacia, bencinera, etc.)
- Voucher/comprobante de pago con tarjeta (crédito o débito) emitido por terminal de pago: Transbank, WebPay, Mercado Pago (punto de venta físico), Stone, Cielo, Rede, Square, SumUp, iZettle, Getnet, u otro — con nombre del comercio y monto
- NFC-e / NF-e / Nota Fiscal Eletrônica brasilera (texto "NFC-e", "NF-e", "Nota Fiscal", CNPJ del emisor, valor total en R$)
- Cupom Fiscal o DANFe de cualquier comercio brasilero
- Recibo impreso de cualquier país con nombre de comercio, monto y medio de pago (VISA, Mastercard, débito, efectivo)

NO ES RECIBO (es_gasto=false) si es:
- Foto de una pantalla de computador o monitor (se ve el monitor físico, borde de pantalla, escritorio de Windows o Mac)
- Panel de administración o backend de tienda online (muestra datos del cliente, IP, navegador, información de gestión interna)
- Confirmación de pedido online o ecommerce ("Tu pedido está confirmado", "Gracias por tu compra", "Pronto recibirás un correo") — NO es boleta
- Carrito de compra online o app (productos pendientes de pago, botón "Pagar" visible)
- Página de producto o tienda online sin confirmación de pago
- Email o captura de pantalla de una app de correo (se ve interfaz de email, fondo oscuro de mail)
- Confirmación de reserva o booking (cancha, hotel, restaurante, evento, vuelo, etc.)
- Pago a Previred, AFP, fondo previsional, seguro, isapre o servicios básicos (agua, luz, gas, internet)
- Comprobante de transferencia bancaria entre personas ("Transferencia exitosa", "Depósito exitoso") — OJO: Mercado Pago como terminal de pago físico SÍ es recibo; solo excluir si es transferencia P2P
- Comprobante de pago bancario (Scotia, BancoEstado, Santander, BCI, Itaú, Falabella u otro banco)
- Cartola o estado de cuenta bancario
- Foto de paisaje, persona, selfie, comida sin comprobante
- Receta médica o prescripción de médico o clínica (aunque tenga RUT del médico o membrete de clínica como Clínica Las Condes, Clínica Alemana, etc.)
- Solicitud o confirmación de reembolso de seguro ("Solicitud de reembolso ingresada", número de solicitud, seguro de salud, isapre, Cámara, etc.)
- Documento de identidad, invitación, flyer, captura de chat o conversación
- Cualquier imagen donde el pago NO es a un comercio por productos o servicios de consumo directo

Responde SOLO con JSON válido (sin markdown, sin texto extra):
{"es_gasto":true/false,"proveedor":"nombre del comercio o null","monto":número o null,"moneda":"CLP o USD o EUR o null","fecha":"YYYY-MM-DD o null","confianza":0.0-1.0}

Si ES recibo, DEBES extraer con máximo esfuerzo:
- proveedor: SOLO el nombre corto del comercio real donde se realizó la compra (ej: "Jumbo", "McDonald's", "Cruz Verde", "OXO"). Máximo 40 caracteres.
  NUNCA uses como proveedor: Transbank, WebPay, Mercado Pago, GetNet, Clover, Square, SumUp, iZettle, Stone, Cielo, Rede — estos son procesadores de pago, no comercios. Si el recibo solo muestra el procesador sin nombre de comercio, usa null.
- monto: monto TOTAL pagado como número (ej: 15990, 45.50) — busca "TOTAL", "Valor total", "Valor pago", "Amount"
- moneda: SOLO usa una de estas 4 opciones: CLP, USD, PEN, BRL
  * CLP → recibo chileno: $ sin país, "pesos", RUT chileno, Transbank, WebPay, comercios chilenos (Jumbo, Copec, Falabella, etc.)
  * PEN → recibo peruano: S/, "soles", SUNAT, RUC peruano, comercios peruanos
  * BRL → recibo brasilero: R$, "reais", NFC-e, CNPJ, comercios brasileros
  * USD → cualquier otro país o moneda (dólares, euros, pesos arg/mex/col, etc.)
- fecha: fecha de la transacción en formato YYYY-MM-DD — busca "Fecha", "Date", "Emissão", no uses la fecha de la foto

Si NO es recibo: {"es_gasto":false,"proveedor":null,"monto":null,"moneda":null,"fecha":null,"confianza":0.9}"""

_PROMPT_EMAIL = """\
Analiza este email/documento y extrae los datos del gasto si existe.

Asunto: {asunto}
De: {de}
Fecha email: {fecha}
Cuerpo:
{cuerpo}

Responde SOLO con JSON válido (sin markdown, sin texto extra):
{{"es_gasto":true/false,"fecha":"YYYY-MM-DD o null","proveedor":"nombre o null","monto":número o null,"moneda":"CLP/USD/EUR/etc o null","descripcion":"descripción breve o null","confianza":0.0-1.0}}

Reglas:
- es_gasto=true solo si es factura, boleta, cobro, receipt o similar
- fecha: del documento, no del email
- moneda: SOLO CLP, USD, PEN o BRL (CLP=Chile, PEN=Perú, BRL=Brasil, USD=resto)
- Si no es gasto: {{"es_gasto":false,"fecha":null,"proveedor":null,"monto":null,"moneda":null,"descripcion":null,"confianza":0}}"""


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    # Quitar bloques markdown ```json ... ```
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    # Extraer solo el bloque JSON si hay texto extra alrededor
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        text = m.group(0)
    # Quitar trailing commas antes de } o ] (JSON inválido común de LLMs)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


def _post(payload: dict, max_retries: int = 4) -> dict:
    """POST a Mistral. Si owa605 se agota (429/402), cambia a owa605.g66 automáticamente."""
    switched = False
    for attempt in range(max_retries):
        resp = requests.post(_API_URL, headers=_get_headers(), json=payload, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 402) and not switched:
            # Cuota agotada — intentar con cuenta fallback
            if _switch_to_fallback():
                switched = True
                logger.info("Reintentando con owa605.g66...")
                continue
            # Sin fallback disponible — esperar y reintentar
            wait = 15 * (attempt + 1)
            logger.warning(f"Rate limit Mistral, esperando {wait}s (intento {attempt+1})")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            logger.warning(f"Rate limit Mistral ({_active_account}), esperando {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError("Mistral: máximo de reintentos agotado")


def extract_from_email(email_data: dict) -> dict:
    """Extrae datos de gasto de un email o foto usando Mistral."""
    adjuntos = email_data.get("adjuntos", [])
    es_foto  = email_data.get("de") == "dropbox" and adjuntos

    try:
        if es_foto:
            adj  = adjuntos[0]
            mime = adj["mime_type"]
            if mime in ("image/heic", "image/heif"):
                mime = "image/jpeg"
            b64      = base64.b64encode(adj["data"]).decode()
            data_url = f"data:{mime};base64,{b64}"

            payload = {
                "model": _MODEL_VISION,
                "temperature": 0.1,
                "max_tokens": 256,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text",      "text": _PROMPT_FOTO},
                    ],
                }],
            }
        else:
            prompt_text = _PROMPT_EMAIL.format(
                asunto=email_data.get("asunto", "")[:300],
                de=email_data.get("de", "")[:150],
                fecha=email_data.get("fecha", "")[:80],
                cuerpo=email_data.get("cuerpo_texto", "")[:2500],
            )
            payload = {
                "model": _MODEL_TEXT,
                "temperature": 0.1,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt_text}],
            }

        data   = _post(payload)
        raw    = data["choices"][0]["message"]["content"]
        result = _parse_json(raw)

        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON inválido de Mistral: {e}")
        return {"es_gasto": False, "error": f"json_parse: {e}"}

    except Exception as e:
        logger.error(f"Error Mistral para {email_data.get('id','?')}: {e}")
        return {"es_gasto": False, "error": str(e)}


def extract_batch(emails_data: list[dict]) -> list[tuple[dict, dict]]:
    """Procesa lista de emails/fotos. Retorna pares (data, extraccion)."""
    results = []
    total   = len(emails_data)
    for i, data in enumerate(emails_data, 1):
        logger.debug(f"Extrayendo {i}/{total}: {data.get('asunto','')[:60]}")
        results.append((data, extract_from_email(data)))
        if i < total:
            time.sleep(1)
    return results
