"""
migrate_to_supabase.py
Migra fotos_cache.db → Supabase BD + thumbnails en Supabase Storage

Estrategia de imágenes:
  - Thumbnails (640px JPEG) → Supabase Storage CDN  → carga rápida en tabla
  - Fotos originales        → Dropbox               → link temporal al hacer clic

Requisitos: solo librerías ya instaladas (requests, dropbox)
Uso:
    source venv/bin/activate
    python migrate_to_supabase.py
"""

import sqlite3, os, sys, time
from pathlib import Path
from dotenv import load_dotenv
import requests
import dropbox
from dropbox.files import ThumbnailFormat, ThumbnailSize

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPA_KEY     = os.getenv("SUPABASE_KEY")
BUCKET       = "recibos"
DB_PATH      = Path(__file__).parent / "fotos_cache.db"

ESTADOS_CON_IMAGEN = {"confirmado", "validado", "recibo", "pendiente", "no_recibo"}

HEADERS = {
    "apikey":        SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

def dropbox_paths_to_try(foto: dict) -> list[str]:
    """Devuelve lista de paths a intentar en Dropbox, en orden de prioridad."""
    original = foto["path"]   # exactamente como está en SQLite
    name     = foto["name"]
    estado   = foto["estado"]
    paths    = []
    if estado == "confirmado":
        paths.append(f"/Recibos/{Path(name).stem}.jpg")  # movido al confirmar
    paths.append(original)    # path original como fallback
    return paths


def get_dropbox():
    return dropbox.Dropbox(
        app_key              = os.getenv("DROPBOX_APP_KEY"),
        app_secret           = os.getenv("DROPBOX_APP_SECRET"),
        oauth2_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN"),
    )


def supa_upsert(table: str, rows: list) -> bool:
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS, json=rows, timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"\n  ✗ {table}: {r.status_code} {r.text[:300]}")
        return False
    return True


def thumbnail_exists(key: str) -> str | None:
    """Devuelve URL si ya existe en storage, None si no."""
    folder, name = key.rsplit("/", 1)
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
        headers={**HEADERS, "Prefer": ""},
        json={"prefix": folder + "/", "search": name, "limit": 1},
        timeout=15,
    )
    if r.status_code == 200 and any(f.get("name") == name for f in r.json()):
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{key}"
    return None


def upload_thumbnail(key: str, data: bytes) -> str | None:
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{key}",
        headers={**HEADERS, "Content-Type": "image/jpeg", "x-upsert": "true"},
        data=data, timeout=30,
    )
    if r.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{key}"
    print(f"\n  ✗ upload {key}: {r.status_code} {r.text[:200]}")
    return None


def migrate():
    dbx  = get_dropbox()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    # 1. Aprendizaje ─────────────────────────────────────────────────────────
    print("\n── Reglas de aprendizaje ───────────────────────────────────────")
    cur.execute("SELECT * FROM aprendizaje")
    reglas = [dict(r) for r in cur.fetchall()]
    if reglas and supa_upsert("aprendizaje", reglas):
        print(f"  ✓ {len(reglas)} reglas")
    elif not reglas:
        print("  (sin reglas)")

    # 2. Leer fotos ──────────────────────────────────────────────────────────
    print("\n── Fotos en SQLite ─────────────────────────────────────────────")
    cur.execute("SELECT * FROM fotos ORDER BY fecha DESC")
    fotos = [dict(r) for r in cur.fetchall()]
    conn.close()

    con_img = [f for f in fotos if f["estado"] in ESTADOS_CON_IMAGEN]
    print(f"  Total: {len(fotos)} | Con thumbnail: {len(con_img)}")

    # 3. Thumbnails → Supabase Storage ───────────────────────────────────────
    print("\n── Thumbnails → Supabase Storage (via Dropbox thumbnail API) ───")
    imagen_urls: dict[str, str] = {}
    ok = skip = err = 0

    for i, foto in enumerate(con_img, 1):
        name = foto["name"]
        key  = f"owa605/thumbs/{name}"
        sys.stdout.write(f"\r  [{i:>3}/{len(con_img)}] {name[:50]:<50}  ok={ok} skip={skip} err={err}")
        sys.stdout.flush()

        # ¿Ya existe?
        url = thumbnail_exists(key)
        if url:
            imagen_urls[foto["path"]] = url
            skip += 1
            continue

        # Pedir thumbnail a Dropbox — prueba cada path posible
        data = None
        tried = dropbox_paths_to_try(foto)
        for dbx_path in tried:
            try:
                _, resp = dbx.files_get_thumbnail(
                    dbx_path,
                    format=ThumbnailFormat.jpeg,
                    size=ThumbnailSize.w640h480,
                )
                data = resp.content
                break
            except dropbox.exceptions.ApiError:
                try:
                    _, resp2 = dbx.files_download(dbx_path)
                    data = resp2.content
                    break
                except Exception:
                    continue
            except Exception:
                continue

        if data is None:
            print(f"\n  ⚠ No encontrada en ningún path: {name} (probé: {tried})")
            err += 1
            continue

        url = upload_thumbnail(key, data)
        if url:
            imagen_urls[foto["path"]] = url
            ok += 1
        else:
            err += 1

        if i % 10 == 0:
            time.sleep(0.3)

    print(f"\n  ✓ Subidos: {ok} | Ya existían: {skip} | Errores: {err}")

    # 4. Fotos → Supabase BD ─────────────────────────────────────────────────
    print("\n── Fotos → Supabase BD ─────────────────────────────────────────")

    def to_row(f: dict) -> dict:
        return {
            "path":         f["path"],
            "name":         f["name"],
            "fecha":        f.get("fecha"),
            "estado":       f["estado"],
            "proveedor":    f.get("proveedor"),
            "monto":        f.get("monto"),
            "moneda":       f.get("moneda"),
            "confianza":    f.get("confianza"),
            "procesado_en": f.get("procesado_en"),
            "cuenta":       f.get("cuenta", "owa605"),
            "editado":      1 if f.get("editado", 0) else 0,
            "fecha_compra": f.get("fecha_compra"),
            "categoria":    f.get("categoria"),
            "imagen_url":   imagen_urls.get(f["path"]),  # thumbnail CDN URL
        }

    BATCH = 200
    total_ok = 0
    for i in range(0, len(fotos), BATCH):
        rows = [to_row(f) for f in fotos[i:i+BATCH]]
        if supa_upsert("fotos", rows):
            total_ok += len(rows)
        print(f"  Batch {i//BATCH+1}: {min(i+BATCH, len(fotos))}/{len(fotos)} ✓")

    # 5. Verificar ────────────────────────────────────────────────────────────
    print("\n── Verificando en Supabase ─────────────────────────────────────")
    r = requests.get(f"{SUPABASE_URL}/rest/v1/fotos?select=estado",
                     headers={**HEADERS, "Prefer": "count=exact"}, timeout=15)
    print(f"  Fotos:  {r.headers.get('content-range', '?')}")
    r2 = requests.get(f"{SUPABASE_URL}/rest/v1/aprendizaje?select=patron",
                      headers={**HEADERS, "Prefer": "count=exact"}, timeout=15)
    print(f"  Reglas: {r2.headers.get('content-range', '?')}")

    size_mb = (ok + skip) * 80 / 1024  # ~80KB por thumbnail JPEG
    print(f"\n══ Migración completa ══════════════════════════════════════════")
    print(f"  Fotos BD:    {total_ok}/{len(fotos)}")
    print(f"  Thumbnails:  {ok+skip} en Supabase (~{size_mb:.0f} MB estimado)")
    print(f"  Originales:  siguen en Dropbox")
    print(f"\n  Dashboard:   https://supabase.com/dashboard/project/lldfjxadijekkvulxumg")


if __name__ == "__main__":
    migrate()
