"""
Clasificación de gastos.
Primero aplica reglas locales (SQLite), luego Gemini si ninguna coincide.
También procesa feedback en lenguaje natural y genera reglas persistentes.
"""
import json
import logging
import os
import sqlite3
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_client     = genai.Client(api_key=_GEMINI_KEY)
_MODEL      = "gemini-2.0-flash"


# ── Helpers de BD ──────────────────────────────────────────────────────────────

def get_categorias(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT id, nombre, descripcion FROM categorias WHERE activa=1 ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "nombre": r[1], "descripcion": r[2]} for r in rows]


def get_reglas(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT id, patron, categoria_id, tipo FROM reglas WHERE activa=1 ORDER BY confianza DESC, usos DESC"
    ).fetchall()
    return [{"id": r[0], "patron": r[1], "categoria_id": r[2], "tipo": r[3]} for r in rows]


def incrementar_uso_regla(db: sqlite3.Connection, regla_id: int):
    db.execute("UPDATE reglas SET usos = usos + 1 WHERE id = ?", (regla_id,))


# ── Clasificación por reglas ───────────────────────────────────────────────────

def _aplicar_reglas(proveedor: str, descripcion: str, reglas: list[dict]) -> Optional[tuple[int, int]]:
    """Retorna (categoria_id, regla_id) o None si no hay match."""
    prov = (proveedor or "").lower().strip()
    desc = (descripcion or "").lower().strip()

    for regla in reglas:
        patron = regla["patron"].lower()
        tipo   = regla["tipo"]
        match  = (
            (tipo == "proveedor_exacto"       and prov == patron)
            or (tipo == "proveedor_contiene"  and patron in prov)
            or (tipo == "descripcion_contiene" and patron in desc)
        )
        if match:
            return regla["categoria_id"], regla["id"]
    return None


# ── Clasificación con IA (Gemini) ──────────────────────────────────────────────

def _clasificar_con_ia(
    proveedor: str, descripcion: str, monto: float, moneda: str,
    categorias: list[dict]
) -> tuple[int, float]:
    """Llama a Gemini para clasificar. Retorna (categoria_id, confianza)."""
    cats_str = "\n".join(f"{c['id']}: {c['nombre']} — {c['descripcion']}" for c in categorias)
    prompt = (
        f'Clasifica este gasto empresarial.\n'
        f'Proveedor: "{proveedor}" | Descripción: "{descripcion}" | Monto: {monto} {moneda}\n\n'
        f'Categorías:\n{cats_str}\n\n'
        f'Responde SOLO con JSON válido (sin markdown): {{"categoria_id":número,"confianza":0.0-1.0}}'
    )
    try:
        resp   = _client.models.generate_content(model=_MODEL, contents=prompt)
        raw    = resp.text.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw   = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        result = json.loads(raw)
        return result["categoria_id"], float(result.get("confianza", 0.7))
    except Exception as e:
        logger.error(f"Error en clasificación IA: {e}")
        otros = next((c for c in categorias if "Otros" in c["nombre"]), categorias[-1])
        return otros["id"], 0.3


# ── Interfaz pública ───────────────────────────────────────────────────────────

def clasificar(
    db: sqlite3.Connection,
    proveedor: str, descripcion: str, monto: float, moneda: str
) -> tuple[int, float, str]:
    """Retorna (categoria_id, confianza, metodo). metodo: 'regla' | 'ia'"""
    reglas = get_reglas(db)
    match  = _aplicar_reglas(proveedor, descripcion, reglas)

    if match:
        cat_id, regla_id = match
        incrementar_uso_regla(db, regla_id)
        logger.info(f"Regla → proveedor='{proveedor}' cat_id={cat_id}")
        return cat_id, 1.0, "regla"

    categorias        = get_categorias(db)
    cat_id, confianza = _clasificar_con_ia(proveedor, descripcion, monto, moneda, categorias)
    logger.info(f"IA → proveedor='{proveedor}' cat_id={cat_id} confianza={confianza:.2f}")
    return cat_id, confianza, "ia"


# ── Feedback en lenguaje natural ───────────────────────────────────────────────

_FEEDBACK_PROMPT = """\
El usuario dio feedback sobre clasificación de gastos. Extrae la regla implícita.

Feedback: "{feedback}"

Categorías disponibles:
{cats}

Responde SOLO con JSON válido (sin markdown):
{{"patron":"texto en minúsculas","categoria_id":número,"tipo":"proveedor_exacto|proveedor_contiene|descripcion_contiene","descripcion_accion":"frase corta"}}

Si no se puede crear una regla clara, devuelve patron=null.\
"""


def procesar_feedback(db: sqlite3.Connection, gasto_id: Optional[int], feedback_texto: str) -> str:
    categorias = get_categorias(db)
    cats_str   = "\n".join(f"{c['id']}: {c['nombre']}" for c in categorias)
    prompt     = _FEEDBACK_PROMPT.format(feedback=feedback_texto, cats=cats_str)

    try:
        resp = _model.generate_content(prompt)
        raw  = resp.text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1])
        result = json.loads(raw)
    except Exception as e:
        logger.error(f"Error procesando feedback: {e}")
        accion = f"Error al procesar feedback: {e}"
        _guardar_feedback(db, gasto_id, feedback_texto, accion)
        return accion

    accion = result.get("descripcion_accion", "Feedback procesado.")

    if result.get("patron") and result.get("categoria_id"):
        patron = result["patron"].lower()
        cat_id = result["categoria_id"]
        tipo   = result["tipo"]
        db.execute(
            "INSERT OR IGNORE INTO reglas (patron, categoria_id, tipo) VALUES (?, ?, ?)",
            (patron, cat_id, tipo),
        )
        _reclasificar_existentes(db, patron, tipo, cat_id)
        db.commit()
        logger.info(f"Regla creada: '{patron}' ({tipo}) → cat {cat_id}")

    _guardar_feedback(db, gasto_id, feedback_texto, accion)
    db.commit()
    return accion


def _guardar_feedback(db: sqlite3.Connection, gasto_id: Optional[int], texto: str, accion: str):
    db.execute(
        "INSERT INTO feedback (gasto_id, feedback_texto, accion_tomada) VALUES (?, ?, ?)",
        (gasto_id, texto, accion),
    )


def _reclasificar_existentes(db: sqlite3.Connection, patron: str, tipo: str, cat_id: int):
    if tipo == "proveedor_exacto":
        n = db.execute(
            "UPDATE gastos SET categoria_id=?, confianza=1.0 WHERE LOWER(proveedor)=? AND revisado=0",
            (cat_id, patron),
        ).rowcount
    elif tipo == "proveedor_contiene":
        n = db.execute(
            "UPDATE gastos SET categoria_id=?, confianza=1.0 WHERE LOWER(proveedor) LIKE ? AND revisado=0",
            (cat_id, f"%{patron}%"),
        ).rowcount
    elif tipo == "descripcion_contiene":
        n = db.execute(
            "UPDATE gastos SET categoria_id=?, confianza=1.0 WHERE LOWER(descripcion) LIKE ? AND revisado=0",
            (cat_id, f"%{patron}%"),
        ).rowcount
    else:
        n = 0
    if n:
        logger.info(f"Re-clasificados {n} gastos existentes con nueva regla '{patron}'")
