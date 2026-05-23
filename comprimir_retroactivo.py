"""
Comprime todos los recibos confirmados en Dropbox/Recibos a 2000px / 80% JPEG.
Ejecutar una vez: python3 comprimir_retroactivo.py
"""
import io, os, time
from pathlib import Path
from dotenv import load_dotenv
from PIL import Image

load_dotenv(Path(__file__).parent / ".env")
import dropbox as dbx_module

dbx = dbx_module.Dropbox(
    app_key=os.getenv("DROPBOX_APP_KEY"),
    app_secret=os.getenv("DROPBOX_APP_SECRET"),
    oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
)
FOLDER = "/Recibos"

def comprimir(data, max_px=2000, quality=80):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        f = max_px / max(w, h)
        img = img.resize((int(w*f), int(h*f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()

result = dbx.files_list_folder(FOLDER)
fotos  = list(result.entries)
while result.has_more:
    result = dbx.files_list_folder_continue(result.cursor)
    fotos += result.entries

total = len(fotos)
print(f"Total recibos en Dropbox: {total}\n")
ok = skip = err = ahorrado_kb = 0

for i, f in enumerate(fotos, 1):
    path = f.path_display
    size = f.size if hasattr(f, "size") else 999999
    if size < 400_000:
        skip += 1
        print(f"[{i}/{total}] ya OK ({size//1024}KB) — {Path(path).name}")
        continue
    try:
        _, resp = dbx.files_download(path)
        data    = resp.content
        orig_kb = len(data) // 1024
        comp    = comprimir(data)
        comp_kb = len(comp) // 1024
        dest    = str(Path(path).parent) + "/" + Path(path).stem + ".jpg"
        dbx.files_upload(comp, dest, mode=dbx_module.files.WriteMode.overwrite)
        if dest.lower() != path.lower():
            try: dbx.files_delete_v2(path)
            except: pass
        ahorrado_kb += orig_kb - comp_kb
        ok += 1
        print(f"[{i}/{total}] ✓ {Path(path).name}: {orig_kb}KB → {comp_kb}KB (-{100-(comp_kb*100//orig_kb)}%)")
        time.sleep(0.3)
    except Exception as e:
        print(f"[{i}/{total}] ✗ {Path(path).name}: {e}")
        err += 1

print(f"\n{'='*50}")
print(f"Listo — {ok} comprimidas, {skip} ya OK, {err} errores")
print(f"Espacio ahorrado: ~{ahorrado_kb//1024} MB")
