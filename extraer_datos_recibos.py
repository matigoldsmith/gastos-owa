"""
Extrae fecha, monto, moneda y comercio de todos los recibos confirmados en Dropbox/Recibos.
Actualiza la BD local con los datos extraídos.

Ejecutar: python3 extraer_datos_recibos.py
"""
import io, os, sqlite3, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import dropbox as dbx_module
from PIL import Image
from extractor import extract_from_email

DB_PATH  = Path(__file__).parent / "fotos_cache.db"
FOLDER   = "/Recibos"

dbx = dbx_module.Dropbox(
    app_key=os.getenv("DROPBOX_APP_KEY"),
    app_secret=os.getenv("DROPBOX_APP_SECRET"),
    oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
)

def comprimir(data, max_px=1600, quality=75):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        f = max_px / max(w, h)
        img = img.resize((int(w*f), int(h*f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()

def db_update(name, proveedor, monto, moneda, fecha):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        UPDATE fotos SET proveedor=?, monto=?, moneda=?, fecha=?
        WHERE name=? AND estado='confirmado'
    """, (proveedor, monto, moneda, fecha, name))
    con.commit()
    con.close()

# Listar recibos en Dropbox
result = dbx.files_list_folder(FOLDER)
fotos  = list(result.entries)
while result.has_more:
    result = dbx.files_list_folder_continue(result.cursor)
    fotos += result.entries

total = len(fotos)
print(f"Recibos a procesar: {total}\n")
ok = skip = err = 0

for i, f in enumerate(fotos, 1):
    path  = f.path_display
    name  = Path(path).name

    # Verificar si ya tiene datos en BD
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT proveedor, monto FROM fotos WHERE name=? AND estado='confirmado'", (name,)).fetchone()
    con.close()
    if row and row[0] and row[1]:
        skip += 1
        print(f"[{i}/{total}] ya tiene datos — {name}")
        continue

    try:
        print(f"[{i}/{total}] descargando {name}...")
        _, resp = dbx.files_download(path)
        data    = comprimir(resp.content)
        print(f"[{i}/{total}] enviando a Mistral ({len(data)//1024}KB)...")
        result  = extract_from_email({
            "id": path, "asunto": name, "de": "dropbox",
            "fecha": "", "cuerpo_texto": "",
            "adjuntos": [{"data": data, "mime_type": "image/jpeg"}],
        })

        # Para recibos ya confirmados por el usuario, extraemos datos aunque
        # Mistral diga es_gasto=false (confiamos en la confirmación del usuario)
        proveedor = result.get("proveedor") or ""
        monto     = result.get("monto")
        moneda    = result.get("moneda") or ""
        fecha     = result.get("fecha") or ""

        if not result.get("es_gasto"):
            print(f"[{i}/{total}] ⚠ Mistral: no recibo, guardando igual → {proveedor or '?'} {monto or '?'} {name}")
        else:
            print(f"[{i}/{total}] ✓ {name} → {proveedor} {monto} {moneda} {fecha}")

        db_update(name, proveedor, monto, moneda, fecha)
        ok += 1
        time.sleep(1)

    except Exception as e:
        print(f"[{i}/{total}] ✗ {name}: {e}")
        err += 1

print(f"\nListo — {ok} procesados, {skip} ya tenían datos, {err} errores")
