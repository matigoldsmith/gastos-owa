"""
supabase_db.py — Reemplaza SQLite con Supabase REST API.
Expone la misma interfaz que usaba review_app.py con sqlite3.
"""
import os
import requests
from datetime import datetime, date

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET       = "recibos"

_H = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}


def _get(table: str, params: dict) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**_H, "Prefer": "count=exact"},
        params=params, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _post(table: str, data, prefer: str = "resolution=merge-duplicates"):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**_H, "Prefer": prefer},
        json=data, timeout=15,
    )
    if r.status_code not in (200, 201):
        raise Exception(f"Supabase {table} POST {r.status_code}: {r.text[:300]}")
    return r


def _patch(table: str, params: dict, data: dict):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=_H, params=params, json=data, timeout=15,
    )
    if r.status_code not in (200, 204):
        raise Exception(f"Supabase {table} PATCH {r.status_code}: {r.text[:300]}")
    return r


# ── Fotos ─────────────────────────────────────────────────────────────────────

def db_get(path: str) -> dict | None:
    rows = _get("fotos", {"path": f"eq.{path}", "select": "*"})
    return rows[0] if rows else None


def db_upsert(path: str, name: str, fecha: str, estado: str,
              proveedor=None, monto=None, moneda=None, confianza=None,
              cuenta: str = "owa605", imagen_url: str = None):
    row = {
        "path":         path,
        "name":         name,
        "fecha":        fecha,
        "estado":       estado,
        "proveedor":    proveedor,
        "monto":        monto,
        "moneda":       moneda,
        "confianza":    confianza,
        "procesado_en": datetime.now().isoformat(),
        "cuenta":       cuenta,
        "imagen_url":   imagen_url,
    }
    _post("fotos", row)


def db_set_estado(path: str, estado: str):
    _patch("fotos", {"path": f"eq.{path}"}, {"estado": estado})


def db_update_by_name(name: str, estados: list[str] | None = None, **fields):
    """Actualiza campos arbitrarios en una foto identificada por name."""
    params = {"name": f"eq.{name}"}
    if estados:
        params["estado"] = f"in.({','.join(estados)})"
    _patch("fotos", params, {**fields, "editado": 1})


def db_set_estado_by_name(name: str, estado_from: str, estado_to: str) -> dict | None:
    """Cambia estado de una foto por name. Devuelve la fila previa o None."""
    rows = _get("fotos", {
        "name":   f"eq.{name}",
        "estado": f"eq.{estado_from}",
        "select": "proveedor,categoria",
    })
    if not rows:
        return None
    _patch("fotos", {"name": f"eq.{name}", "estado": f"eq.{estado_from}"}, {"estado": estado_to})
    return rows[0]


def db_get_by_name_estados(name: str, estados: list[str]) -> dict | None:
    rows = _get("fotos", {
        "name":   f"eq.{name}",
        "estado": f"in.({','.join(estados)})",
        "select": "path,name,proveedor,monto,moneda,fecha,categoria,editado,imagen_url",
    })
    return rows[0] if rows else None


# ── Aprendizaje ───────────────────────────────────────────────────────────────

def get_reglas() -> list[tuple]:
    """Devuelve [(patron, nombre_correcto, categoria), ...]."""
    rows = _get("aprendizaje", {
        "select": "patron,nombre_correcto,categoria",
        "order":  "usos.desc",
    })
    return [(r["patron"], r.get("nombre_correcto"), r.get("categoria")) for r in rows]


def guardar_aprendizaje(proveedor: str, categoria: str):
    if not proveedor or not categoria:
        return
    existing = _get("aprendizaje", {"patron": f"eq.{proveedor}", "select": "usos"})
    if existing:
        new_usos = (existing[0].get("usos") or 1) + 1
        _patch("aprendizaje", {"patron": f"eq.{proveedor}"},
               {"categoria": categoria, "usos": new_usos})
    else:
        _post("aprendizaje", {
            "patron":          proveedor,
            "nombre_correcto": proveedor,
            "categoria":       categoria,
            "creado_en":       datetime.now().isoformat(),
        }, prefer="")


def get_aprendizaje_all() -> list[dict]:
    return _get("aprendizaje", {
        "select": "patron,nombre_correcto,categoria,usos,creado_en",
        "order":  "usos.desc",
    })


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    hoy        = date.today()
    inicio_mes = f"{hoy.year}-{hoy.month:02d}-01"

    # Conteos por estado (trae solo el campo estado, sin límite)
    rows = _get("fotos", {"select": "estado"})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["estado"]] = counts.get(r["estado"], 0) + 1

    # Llamadas IA este mes
    mes_rows = _get("fotos", {
        "select":       "cuenta",
        "estado":       "in.(recibo,no_recibo,borrado)",
        "procesado_en": f"gte.{inicio_mes}",
    })
    cuentas: dict[str, int] = {}
    for r in mes_rows:
        cuentas[r["cuenta"]] = cuentas.get(r["cuenta"], 0) + 1

    auto_mes   = len(_get("fotos", {"select": "path", "estado": "eq.borrado",
                                     "cuenta": "eq.auto", "procesado_en": f"gte.{inicio_mes}"}))
    auto_total = len(_get("fotos", {"select": "path", "estado": "eq.borrado", "cuenta": "eq.auto"}))

    return {
        "counts":          counts,
        "cuentas":         cuentas,
        "auto_borr_mes":   auto_mes,
        "auto_borr_total": auto_total,
        "hoy":             hoy,
    }


# ── Listados ──────────────────────────────────────────────────────────────────

def get_confirmados() -> list[dict]:
    return _get("fotos", {
        "estado": "eq.confirmado",
        "select": "path,name,proveedor,monto,moneda,fecha,categoria,editado,imagen_url",
        "order":  "fecha.desc",
    })


def get_validados() -> list[dict]:
    return _get("fotos", {
        "estado": "eq.validado",
        "select": "path,name,proveedor,monto,moneda,fecha,categoria,editado",
        "order":  "fecha.desc,procesado_en.desc",
    })


# ── Storage helpers ───────────────────────────────────────────────────────────

def upload_thumbnail(name: str, data: bytes, mime: str = "image/jpeg") -> str:
    """Sube thumbnail a Supabase Storage. Devuelve URL pública."""
    key = f"owa605/thumbs/{name}"
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}",
        headers={**_H, "Content-Type": mime, "x-upsert": "true"},
        data=data, timeout=30,
    )
    if r.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{key}"
    raise Exception(f"Storage upload error: {r.status_code} {r.text[:200]}")
