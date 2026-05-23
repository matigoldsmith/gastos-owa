"""
Web app para revisar fotos de Dropbox y clasificarlas como recibo o no.
  → abrir http://localhost:5001
"""
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import io

import dropbox as dbx_module
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template_string, request
from PIL import Image

import supabase_db as sdb

load_dotenv()

APP_KEY        = os.getenv("DROPBOX_APP_KEY")
APP_SECRET     = os.getenv("DROPBOX_APP_SECRET")
REFRESH_TOKEN  = os.getenv("DROPBOX_REFRESH_TOKEN")
FOLDER         = os.getenv("DROPBOX_FOLDER", "")
FOLDER_RECIBOS = "/Recibos"

IMAGE_EXTS  = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
EXT_TO_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "heic": "image/heic", "heif": "image/heif"}

THUMB_DIR  = Path(__file__).parent / "thumb_cache"
THUMB_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("dropbox").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# ── Base de datos ──────────────────────────────────────────────────────────────

def comprimir_imagen(data: bytes, max_px: int = 1600, quality: int = 65) -> bytes:
    """Comprime imagen a max_px en el lado más largo y calidad JPEG dada."""
    try:
        from PIL import ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # corrige rotación EXIF automáticamente
        img = img.convert("RGB")  # elimina canal alpha, soporta HEIC convertido
        w, h = img.size
        if max(w, h) > max_px:
            factor = max_px / max(w, h)
            img = img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[compresión] Error, usando original: {e}")
        return data


CATEGORIAS = [
    "Viajes",
    "Representación",
    "Gastos Varios de Oficina",
    "Servicios Profesionales",
    "Cuentas",
    "Otros",
]

# Keywords para auto-categorización por defecto
_CAT_KEYWORDS = {
    "Viajes": [
        "copec","shell","petrobras","enex","terpel","bencin","gasolina","combustible",
        "estacionamiento","parking","peaje","autopista","autopass","tag ","zencillo",
        "rent a car","hertz","europcar","avis","budget","sixt",
    ],
    "Representación": [
        "mcdonald","burger king","kfc","subway","wendy","pizza","sushi","ceviche",
        "restauran","restaurant","bistro","grill","parrilla","cafeteria","café","cafe",
        "starbucks","dunkin","jugo","almuerzo","cena","desayuno","bar ","taberna",
        "frisby","pollo campero","taco","burritos","domino",
    ],
    "Gastos Varios de Oficina": [
        "jumbo","lider","walmart","unimarc","tottus","santa isabel","ekono",
        "oxxo","aramark","sodexo","real food","seven eleven","ok market",
        "supermercado","minimarket","almacen","ferreteria","easy","sodimac",
    ],
    "Servicios Profesionales": [
        "contador","contabilidad","abogado","notaria","auditoria","consultoria",
        "asesor","estudio juridico","servicios profesionales",
    ],
    "Cuentas": [
        "enel","chilectra","aguas andinas","esval","metrogas","entel","movistar",
        "wom","claro","vtr","gtd","electricidad","agua potable","gas natural",
        "previred","afp","isapre","fonasa","internet","telefonia",
    ],
}

def auto_categorizar(proveedor: str) -> str:
    """Categoría por keywords. Primero revisa reglas aprendidas, luego keywords."""
    if not proveedor or proveedor in ("[ilegible]", "[sin_archivo]"):
        return "Otros"
    p = proveedor.lower()
    # Reglas aprendidas primero
    for patron, _, cat in sdb.get_reglas():
        if cat and (patron.lower() in p or p in patron.lower()):
            return cat
    # Keywords por defecto
    for cat, kws in _CAT_KEYWORDS.items():
        for kw in kws:
            if kw in p:
                return cat
    return "Otros"


def init_db():
    logger.info("Usando Supabase como BD — init_db() no requerido")


def db_get(path: str) -> dict | None:
    return sdb.db_get(path)


def db_upsert(path: str, name: str, fecha: str, estado: str,
              proveedor: str = None, monto=None, moneda: str = None, confianza: float = None,
              cuenta: str = None):
    from extractor import get_active_account
    cuenta = cuenta or get_active_account()
    sdb.db_upsert(path, name, fecha, estado, proveedor=proveedor, monto=monto,
                  moneda=moneda, confianza=confianza, cuenta=cuenta)


def db_set_estado(path: str, estado: str):
    sdb.db_set_estado(path, estado)


# ── Dropbox ────────────────────────────────────────────────────────────────────

def get_dbx():
    return dbx_module.Dropbox(
        app_key=APP_KEY, app_secret=APP_SECRET, oauth2_refresh_token=REFRESH_TOKEN
    )


def _list_folder(dbx, folder):
    photos = []
    try:
        res = dbx.files_list_folder(folder, limit=100)
        while True:
            for entry in res.entries:
                if not isinstance(entry, dbx_module.files.FileMetadata):
                    continue
                ext = Path(entry.name).suffix.lower().lstrip(".")
                if ext not in IMAGE_EXTS:
                    continue
                photos.append({
                    "path":  entry.path_lower,
                    "name":  entry.name,
                    "fecha": entry.client_modified.strftime("%Y-%m-%d") if entry.client_modified else "",
                })
            if res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
            else:
                break
    except dbx_module.exceptions.ApiError:
        pass
    return photos


# ── API ────────────────────────────────────────────────────────────────────────

@app.route("/api/photos")
def api_photos():
    try:
        dbx = get_dbx()
        raw = _list_folder(dbx, FOLDER)
        photos = []
        for p in raw:
            cached = db_get(p["path"])
            estado = cached["estado"] if cached else "pendiente"
            # Ignorar fotos ya confirmadas o borradas
            if estado in ("confirmado", "borrado"):
                continue

            # Fotos de 2025 o antes → borrar directo sin clasificar ni mostrar
            anio_match = re.match(r'^(\d{4})', p["name"])
            if anio_match and int(anio_match.group(1)) <= 2025:
                try:
                    dbx.files_delete_v2(p["path"])
                except Exception:
                    pass
                db_upsert(p["path"], p["name"], p["fecha"], "borrado", cuenta="auto")
                logger.info(f"[auto-borrado] {p['name']}")
                continue

            entry = {
                "path":      p["path"],
                "name":      p["name"],
                "fecha":     p["fecha"],
                "estado":    estado,
                "proveedor": cached["proveedor"] if cached else None,
                "monto":     cached["monto"]     if cached else None,
                "moneda":    cached["moneda"]    if cached else None,
                "confianza": cached["confianza"] if cached else None,
            }
            photos.append(entry)
            if not cached:
                db_upsert(p["path"], p["name"], p["fecha"], "pendiente")

        # Ordenar: clasificadas primero, luego pendientes de más reciente a más antigua
        clasificadas = [p for p in photos if p["estado"] != "pendiente"]
        pendientes   = sorted([p for p in photos if p["estado"] == "pendiente"],
                               key=lambda p: p["fecha"] or "", reverse=True)
        photos = clasificadas + pendientes

        return jsonify({"photos": photos, "total": len(photos)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats")
def api_stats():
    """Conteos rápidos desde Supabase, sin tocar Dropbox."""
    st      = sdb.get_stats()
    counts  = st["counts"]
    cuentas = st["cuentas"]
    hoy     = st["hoy"]
    limite  = 2000
    c1      = cuentas.get("owa605", 0)
    c2      = cuentas.get("owa605.g66", 0)
    llamadas_mes = c1 + c2
    from extractor import get_active_account
    return jsonify({
        "confirmados":         counts.get("confirmado", 0),
        "borrados":            counts.get("borrado", 0),
        "recibos":             counts.get("recibo", 0),
        "no_recibo":           counts.get("no_recibo", 0),
        "pendiente":           counts.get("pendiente", 0),
        "mistral_mes":         llamadas_mes,
        "mistral_limite":      limite,
        "mistral_c1":          c1,
        "mistral_c2":          c2,
        "mistral_pct":         round(llamadas_mes / limite * 100, 1),
        "mes":                 hoy.strftime("%b %Y"),
        "mistral_cuenta":      get_active_account(),
        "auto_borrados_mes":   st["auto_borr_mes"],
        "auto_borrados_total": st["auto_borr_total"],
    })


@app.route("/api/comprimir-retroactivo", methods=["POST"])
def api_comprimir_retroactivo():
    """Comprime en background todos los recibos ya confirmados en Dropbox."""
    def _run():
        import time as _t
        dbx    = get_dbx()
        fotos  = _list_folder(dbx, FOLDER_RECIBOS)
        total  = len(fotos)
        logger.info(f"[retro] Iniciando compresión de {total} recibos confirmados...")
        ok = skip = err = ahorrado_kb = 0
        for p in fotos:
            path = p["path"]
            # Saltar si ya es .jpg pequeño (ya comprimido)
            try:
                meta = dbx.files_get_metadata(path)
                if hasattr(meta, 'size') and meta.size < 500_000:
                    skip += 1
                    continue
            except Exception:
                pass
            try:
                _, response = dbx.files_download(path)
                data        = response.content
                orig_kb     = len(data) // 1024
                compressed  = comprimir_imagen(data, max_px=2000, quality=80)
                comp_kb     = len(compressed) // 1024
                dest        = Path(path).parent.as_posix() + "/" + Path(path).stem + ".jpg"
                dbx.files_upload(compressed, dest, mode=dbx_module.files.WriteMode.overwrite)
                if dest != path:
                    try: dbx.files_delete_v2(path)
                    except Exception: pass
                ahorrado_kb += orig_kb - comp_kb
                ok += 1
                logger.info(f"[retro] {p['name']} {orig_kb}KB → {comp_kb}KB")
                _t.sleep(0.3)
            except Exception as e:
                logger.error(f"[retro] Error en {p['name']}: {e}")
                err += 1
        logger.info(f"[retro] Listo — {ok} comprimidas, {skip} ya OK, {err} errores. Espacio ahorrado: ~{ahorrado_kb//1024} MB")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "Compresión retroactiva iniciada — ver logs en terminal"})


@app.route("/api/recibos-confirmados")
def api_recibos_confirmados():
    try:
        rows = sdb.get_confirmados()
        photos = []
        for r in rows:
            photos.append({
                "path":      r.get("path", ""),
                "name":      r.get("name", ""),
                "fecha":     r.get("fecha", ""),
                "proveedor": r.get("proveedor"),
                "monto":     r.get("monto"),
                "moneda":    r.get("moneda") or "CLP",
                "categoria": r.get("categoria") or auto_categorizar(r.get("proveedor") or ""),
                "editado":   bool(r.get("editado", 0)),
                "imagen_url": r.get("imagen_url"),
            })
        return jsonify({"photos": photos, "total": len(photos)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _rotar_jpeg(data: bytes, max_px: int = 800) -> bytes:
    """Aplica rotación EXIF y redimensiona. Siempre devuelve JPEG."""
    from PIL import ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        f = max_px / max(w, h)
        img = img.resize((int(w * f), int(h * f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


@app.route("/thumb")
def serve_thumb():
    """Thumbnail: primero busca en Supabase Storage (CDN), luego caché disco, luego Dropbox."""
    path = request.args.get("path")
    if not path:
        return "path requerido", 400

    # 1. ¿Foto tiene imagen_url en Supabase? → redirect al CDN
    foto = sdb.db_get(path)
    if foto and foto.get("imagen_url"):
        from flask import redirect
        return redirect(foto["imagen_url"])

    # 2. Caché disco local
    import hashlib
    cache_key  = "v2_" + hashlib.md5(path.encode()).hexdigest() + ".jpg"
    cache_file = THUMB_DIR / cache_key
    if cache_file.exists():
        return Response(
            cache_file.read_bytes(), mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    # 3. Dropbox thumbnail API
    try:
        dbx     = get_dbx()
        _, resp = dbx.files_get_thumbnail(
            path,
            format=dbx_module.files.ThumbnailFormat.jpeg,
            size=dbx_module.files.ThumbnailSize.w480h320,
        )
        data = _rotar_jpeg(resp.content, max_px=800)
        cache_file.write_bytes(data)
        # Subir a Supabase para la próxima vez
        try:
            name = Path(path).name
            url  = sdb.upload_thumbnail(name, data)
            sdb.db_set_estado(path, foto["estado"] if foto else "pendiente")  # mantiene estado
            import requests as _req
            _req.patch(
                f"{sdb.SUPABASE_URL}/rest/v1/fotos",
                headers={**sdb._H}, params={"path": f"eq.{path}"},
                json={"imagen_url": url}, timeout=10,
            )
        except Exception:
            pass
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return serve_img_for(path)


@app.route("/img")
def serve_img():
    path = request.args.get("path")
    if not path:
        return "path requerido", 400
    return serve_img_for(path)


def serve_img_for(path: str):
    try:
        dbx     = get_dbx()
        _, resp = dbx.files_download(path)
        data    = _rotar_jpeg(resp.content, max_px=1600)
        return Response(data, mimetype="image/jpeg")
    except Exception as e:
        return str(e), 500


@app.route("/api/classify", methods=["POST"])
def api_classify():
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "path requerido"}), 400
    try:
        # Fotos de 2025 o antes → borrar directamente sin gastar tokens de IA
        nombre = Path(path).name
        anio_match = re.match(r'^(\d{4})', nombre)
        if anio_match and int(anio_match.group(1)) <= 2025:
            try:
                get_dbx().files_delete_v2(path)
            except Exception:
                pass
            db_upsert(path, nombre, "", "borrado", cuenta="auto")
            logger.info(f"[auto-borrado] {nombre}")
            return jsonify({"es_recibo": False, "auto_borrado": True, "cached": False})

        # Verificar si ya está en caché
        cached = db_get(path)
        if cached and cached["estado"] in ("recibo", "no_recibo"):
            logger.info(f"[caché] {Path(path).name} → {cached['estado']}")
            return jsonify({
                "es_recibo": cached["estado"] == "recibo",
                "confianza": cached["confianza"],
                "proveedor": cached["proveedor"],
                "monto":     cached["monto"],
                "moneda":    cached["moneda"],
                "cached":    True,
            })

        dbx         = get_dbx()
        logger.info(f"[clasificando] {Path(path).name} → descargando...")
        _, response = dbx.files_download(path)
        data        = response.content
        orig_kb     = len(data) // 1024
        data        = comprimir_imagen(data, max_px=1200, quality=75)  # suficiente para clasificar
        comp_kb     = len(data) // 1024
        logger.info(f"[clasificando] {Path(path).name} → comprimido {orig_kb}KB → {comp_kb}KB, enviando a Mistral...")

        from extractor import extract_from_email
        result = extract_from_email({
            "id": path, "asunto": Path(path).name, "de": "dropbox",
            "fecha": "", "cuerpo_texto": "",
            "adjuntos": [{"data": data, "mime_type": "image/jpeg"}],
        })

        es_recibo = result.get("es_gasto", False)
        estado    = "recibo" if es_recibo else "no_recibo"
        name      = Path(path).name
        proveedor = result.get("proveedor") or ""
        monto     = result.get("monto")
        moneda    = result.get("moneda") or ""
        conf      = result.get("confianza", 0)
        icono     = "✓ RECIBO" if es_recibo else "✕ no recibo"
        detalle   = f"{proveedor} {monto} {moneda}".strip() if proveedor else ""
        logger.info(f"[Mistral] {name} → {icono}{(' — ' + detalle) if detalle else ''} (confianza {conf:.0%})")

        db_upsert(
            path, name, "", estado,
            proveedor=result.get("proveedor"),
            monto=result.get("monto"),
            moneda=result.get("moneda"),
            confianza=conf,
        )

        return jsonify({
            "es_recibo": es_recibo,
            "confianza": result.get("confianza", 0),
            "proveedor": result.get("proveedor", ""),
            "monto":     result.get("monto"),
            "moneda":    result.get("moneda", ""),
            "cached":    False,
        })
    except Exception as e:
        logger.error(f"Error clasificando {path}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "path requerido"}), 400
    try:
        dbx      = get_dbx()
        filename = Path(path).stem + ".jpg"
        dest     = f"{FOLDER_RECIBOS}/{filename}"
        # Descargar, comprimir y subir — luego borrar original
        _, response = dbx.files_download(path)
        data     = response.content
        orig_kb  = len(data) // 1024
        data     = comprimir_imagen(data, max_px=2000, quality=80)  # calidad OCR-ready
        comp_kb  = len(data) // 1024
        dbx.files_upload(data, dest, mode=dbx_module.files.WriteMode.add)
        dbx.files_delete_v2(path)
        db_set_estado(path, "confirmado")
        logger.info(f"[confirmado] {filename} — {orig_kb}KB → {comp_kb}KB ({100-comp_kb*100//orig_kb}% menos)")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error confirmando {path}: {e}")
        return jsonify({"error": str(e)}), 500


def limpiar_fotos_antiguas():
    """Borra de Dropbox (en background) todas las fotos anteriores a 2026-01-01."""
    CORTE = "2026-01-01"
    try:
        dbx   = get_dbx()
        fotos = _list_folder(dbx, FOLDER)
        borradas = 0
        for p in fotos:
            if (p["fecha"] or "") < CORTE:
                try:
                    dbx.files_delete_v2(p["path"])
                    db_upsert(p["path"], p["name"], p["fecha"], "borrado")
                    borradas += 1
                except Exception:
                    pass
        print(f"[limpieza] {borradas} fotos anteriores a {CORTE} borradas de Dropbox.")
    except Exception as e:
        print(f"[limpieza] Error: {e}")


@app.route("/api/delete", methods=["POST"])
def api_delete():
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "path requerido"}), 400
    try:
        get_dbx().files_delete_v2(path)
        db_set_estado(path, "borrado")
        logger.info(f"Eliminada: {path}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error eliminando {path}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/borrar-confirmado", methods=["POST"])
def api_borrar_confirmado():
    """Borra un recibo ya confirmado: lo elimina de /Recibos y marca como borrado en BD."""
    name = request.json.get("name")
    if not name:
        return jsonify({"error": "name requerido"}), 400
    path_recibo = f"{FOLDER_RECIBOS}/{name}"
    try:
        try:
            get_dbx().files_delete_v2(path_recibo)
        except Exception:
            pass
        sdb._patch("fotos", {"name": f"eq.{name}", "estado": "eq.confirmado"}, {"estado": "borrado"})
        logger.info(f"[borrado] recibo confirmado eliminado: {name}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[borrado] Error en {name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/confirm-batch", methods=["POST"])
def api_confirm_batch():
    from concurrent.futures import ThreadPoolExecutor
    paths = request.json.get("paths", [])
    if not paths:
        return jsonify({"ok": True, "count": 0})
    dbx = get_dbx()
    errors = []
    def _confirm_one(path):
        try:
            filename = Path(path).stem + ".jpg"
            dest     = f"{FOLDER_RECIBOS}/{filename}"
            _, response = dbx.files_download(path)
            data     = comprimir_imagen(response.content, max_px=2000, quality=80)  # OCR-ready
            dbx.files_upload(data, dest, mode=dbx_module.files.WriteMode.add)
            dbx.files_delete_v2(path)
            db_set_estado(path, "confirmado")
            logger.info(f"[confirmado] {filename} → {len(data)//1024}KB")
        except Exception as e:
            errors.append(str(e))
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_confirm_one, paths))
    return jsonify({"ok": True, "count": len(paths) - len(errors), "errors": errors})


@app.route("/api/delete-batch", methods=["POST"])
def api_delete_batch():
    from concurrent.futures import ThreadPoolExecutor
    paths = request.json.get("paths", [])
    if not paths:
        return jsonify({"ok": True, "count": 0})
    dbx = get_dbx()
    errors = []
    def _delete_one(path):
        try:
            dbx.files_delete_v2(path)
            db_set_estado(path, "borrado")
        except Exception as e:
            errors.append(str(e))
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_delete_one, paths))
    return jsonify({"ok": True, "count": len(paths) - len(errors), "errors": errors})


@app.route("/api/editar-recibo", methods=["POST"])
def api_editar_recibo():
    """Edita datos de un recibo (sin aprendizaje — aprendizaje solo al validar gasto)."""
    data      = request.json
    name      = data.get("name")
    proveedor = (data.get("proveedor") or "").strip()
    monto     = data.get("monto")
    moneda    = (data.get("moneda") or "").strip()
    fecha     = (data.get("fecha") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name requerido"}), 400

    sdb.db_update_by_name(name, estados=["confirmado", "validado"],
                          proveedor=proveedor, monto=monto, moneda=moneda,
                          fecha=fecha, categoria=categoria)
    return jsonify({"ok": True})


@app.route("/api/editar-lote", methods=["POST"])
def api_editar_lote():
    """Aplica categoría (y/o proveedor) a varios recibos — SIN aprendizaje (solo edición)."""
    data      = request.json
    names     = data.get("names", [])
    categoria = data.get("categoria")
    proveedor = data.get("proveedor")

    if not names:
        return jsonify({"ok": False, "error": "sin recibos"}), 400

    actualizados = 0
    for name in names:
        row = sdb.db_get_by_name_estados(name, ["confirmado", "validado"])
        if not row:
            continue
        fields = {}
        if proveedor: fields["proveedor"] = proveedor
        if categoria: fields["categoria"] = categoria
        if fields:
            sdb.db_update_by_name(name, **fields)
            actualizados += 1

    return jsonify({"ok": True, "actualizados": actualizados})


@app.route("/api/validar-gasto", methods=["POST"])
def api_validar_gasto():
    """Confirma o rechaza gastos identificados por IA. El aprendizaje SOLO ocurre aquí."""
    data   = request.json
    names  = data.get("names", [])
    accion = data.get("accion")  # "confirmar" | "rechazar"

    if not names or accion not in ("confirmar", "rechazar"):
        return jsonify({"ok": False, "error": "parámetros inválidos"}), 400

    nuevo_estado = "validado" if accion == "confirmar" else "rechazado_gasto"
    actualizados = 0
    for name in names:
        row = sdb.db_set_estado_by_name(name, "confirmado", nuevo_estado)
        if not row:
            continue
        actualizados += 1
        if accion == "confirmar":
            sdb.guardar_aprendizaje(row.get("proveedor"), row.get("categoria"))
            logger.info(f"[validar] ✓ {name} → validado | {row.get('proveedor')} / {row.get('categoria')}")
        else:
            logger.info(f"[validar] ✗ {name} → rechazado_gasto")

    return jsonify({"ok": True, "actualizados": actualizados})


@app.route("/api/gastos-validados")
def api_gastos_validados():
    """Retorna gastos confirmados por el usuario (estado=validado)."""
    try:
        rows = sdb.get_validados()
        result = [{
            "path":      r.get("path", ""),
            "name":      r.get("name", ""),
            "proveedor": r.get("proveedor"),
            "monto":     r.get("monto"),
            "moneda":    r.get("moneda") or "CLP",
            "fecha":     r.get("fecha"),
            "categoria": r.get("categoria") or "Otros",
            "editado":   bool(r.get("editado", 0)),
        } for r in rows]
        return jsonify({"gastos": result, "total": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/aprendizaje")
def api_aprendizaje():
    rows = sdb.get_aprendizaje_all()
    return jsonify({"reglas": [{"patron": r["patron"], "correcto": r.get("nombre_correcto"),
                                "categoria": r.get("categoria"), "usos": r.get("usos"),
                                "creado_en": r.get("creado_en")} for r in rows]})


_PROCESADORES_PAGO = {
    "transbank", "webpay", "mercado pago", "mercadopago",
    "getnet", "clover", "square", "sumup", "izzettle", "stone",
    "cielo", "rede", "pagofácil", "pagofacil",
}

def limpiar_proveedor(proveedor: str) -> str:
    """Elimina proveedores que son procesadores de pago, no comercios."""
    if not proveedor:
        return proveedor
    if proveedor.lower().strip() in _PROCESADORES_PAGO:
        logger.info(f"[limpieza] Proveedor '{proveedor}' es procesador de pago → null")
        return ""
    return proveedor


def aplicar_aprendizaje(proveedor: str) -> str:
    """Aplica reglas aprendidas al proveedor extraído por Mistral."""
    if not proveedor:
        return proveedor
    prov_lower = proveedor.lower()
    for patron, correcto, _ in sdb.get_reglas():
        if correcto and (patron.lower() in prov_lower or prov_lower in patron.lower()):
            return correcto
    return proveedor


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Gastos OWA</title>
<style>
/* ═══════════════════════════════════════════
   GASTOS OWA — Design System v2
   Clean · Minimal · Professional
   ═══════════════════════════════════════════ */

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  font-size: 13px;
  background: #f7f7f9;
  color: #111118;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ─────────────────────────────── */
header {
  background: #fff;
  border-bottom: 1px solid #e8e8ed;
  padding: 0 24px;
  display: flex;
  align-items: stretch;
  gap: 20px;
  position: sticky;
  top: 0;
  z-index: 100;
  height: 50px;
}

header h1 {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.3px;
  white-space: nowrap;
  display: flex;
  align-items: center;
  margin-right: 4px;
}
header h1 span.acc { color: #4f46e5; }
header h1 span.sec { color: #111118; }

/* ── Tabs ───────────────────────────────── */
.tabs { display: flex; gap: 0; align-items: stretch; }

.tab {
  padding: 0 14px;
  font-size: 13px;
  cursor: pointer;
  color: #999;
  display: flex;
  align-items: center;
  gap: 7px;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: color .15s, border-color .15s;
}
.tab:hover { color: #444; }
.tab.active { color: #111118; font-weight: 600; border-bottom-color: #4f46e5; }

.tab .badge {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 10px;
  background: #efeff5;
  color: #999;
  min-width: 22px;
  text-align: center;
  line-height: 1.6;
}
.tab.active .badge { background: #4f46e5; color: #fff; }

/* ── Header actions ─────────────────────── */
.hdr-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }

.hbtn {
  font-size: 12px;
  padding: 5px 12px;
  border: 1px solid #e5e5ea;
  border-radius: 6px;
  cursor: pointer;
  background: transparent;
  color: #666;
  white-space: nowrap;
  transition: all .15s;
}
.hbtn:hover { background: #f5f5f8; color: #333; border-color: #ccc; }

#status { font-size: 12px; color: #bbb; }

.mistral-pill {
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 20px;
  background: #f0f0fe;
  color: #4f46e5;
  border: 1px solid #d4d4fb;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.mk { font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 600; }
.mk-ok    { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
.mk-warn  { background: #fef9c3; color: #854d0e; border: 1px solid #fde68a; }
.mk-agot  { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
.mk-idle  { background: #f4f4f5; color: #a1a1aa; border: 1px solid #e4e4e7; }

/* ── Pages ──────────────────────────────── */
.page { display: none; padding: 20px 24px; }
.page.active { display: block; }

/* ── Sections ───────────────────────────── */
.seccion { margin-bottom: 28px; }
.sec-hdr {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 10px;
  border-bottom: 1px solid #eaeaef;
  margin-bottom: 14px;
}
.sec-hdr h2 {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .6px;
  flex: 1;
  color: #aaa;
}
.sec-hdr .n { font-size: 12px; color: #ccc; }
.sec-hdr button {
  font-size: 12px;
  padding: 4px 12px;
  border: 1px solid #e5e5ea;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 500;
  transition: all .15s;
  background: #fff;
  color: #666;
}
.sec-hdr button:hover { background: #f5f5f8; border-color: #ccc; }
.btn-sel   { color: #4f46e5 !important; background: #f0f0fe !important; border-color: #c7d2fe !important; }
.btn-verde { background: #4f46e5 !important; color: #fff !important; border-color: #4f46e5 !important; }
.btn-rojo  { background: #dc2626 !important; color: #fff !important; border-color: #dc2626 !important; }

/* ── Grid ───────────────────────────────── */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: 10px; }

/* ── Cards (Calibrar) ───────────────────── */
.card {
  background: #fff;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid #e8e8ed;
  position: relative;
  cursor: pointer;
  transition: box-shadow .15s, border-color .15s;
}
.card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.07); border-color: #ccc; }
.card.sel { border-color: #4f46e5; box-shadow: 0 0 0 2px #e0e0fe; }
.card .chk {
  position: absolute; top: 6px; left: 6px;
  width: 18px; height: 18px;
  background: rgba(255,255,255,.92);
  border: 1.5px solid #ccc;
  border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; z-index: 2; transition: all .1s;
}
.card.sel .chk { background: #4f46e5; border-color: #4f46e5; color: #fff; }
.card img { width: 100%; height: 128px; object-fit: cover; display: block; background: #f7f7f9; }
.card .info { padding: 8px 9px; }
.card .det  { font-size: 11px; color: #888; margin-bottom: 6px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card .acts { display: flex; gap: 4px; }
.card button {
  flex: 1; padding: 4px 3px; font-size: 11px; border-radius: 5px;
  cursor: pointer; border: 1px solid #e8e8ed; background: #f7f7f9;
  color: #666; transition: all .1s;
}
.card button.cv { color: #4f46e5; border-color: #c7d2fe; background: #f0f0fe; }
.card button.cv:hover { background: #e0e0fe; }
.card button.cr { color: #dc2626; border-color: #fecaca; background: #fef2f2; }
.card button.cr:hover { background: #fee2e2; }

.empty-msg { text-align: center; padding: 60px 20px; color: #ccc; font-size: 13px; }

/* ── Float bar ──────────────────────────── */
#float-bar {
  display: none;
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  background: #18181b; color: #fff;
  border-radius: 12px; padding: 10px 16px;
  box-shadow: 0 4px 24px rgba(0,0,0,.3);
  z-index: 200;
  align-items: center; gap: 10px; white-space: nowrap;
}
#float-bar.visible { display: flex; }
#float-bar .float-n { font-size: 13px; font-weight: 600; }
#float-bar button { font-size: 12px; padding: 5px 14px; border: none; border-radius: 7px;
                    cursor: pointer; font-weight: 500; }
#float-bar .fb-verde { background: #4f46e5; color: #fff; }
#float-bar .fb-rojo  { background: #dc2626; color: #fff; }
#float-bar .fb-cancel { background: rgba(255,255,255,.1); color: rgba(255,255,255,.7); }

/* ── Lightbox ───────────────────────────── */
#lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.88);
            z-index: 1000; align-items: center; justify-content: center; cursor: zoom-out; }
#lightbox.open { display: flex; }
#lightbox img { max-width: 90vw; max-height: 90vh; border-radius: 8px; object-fit: contain; }

/* ── Botones fila tabla ─────────────────── */
.btn-confirmar-fila {
  padding: 3px 11px;
  border-radius: 6px;
  background: #f0f0fe;
  color: #4f46e5;
  border: 1px solid #c7d2fe;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all .15s;
  white-space: nowrap;
}
.btn-confirmar-fila:hover { background: #4f46e5; color: #fff; border-color: #4f46e5; }

.btn-rechazar-fila {
  padding: 3px 8px;
  border-radius: 6px;
  background: transparent;
  color: #ccc;
  border: 1px solid #e8e8ed;
  font-size: 11px;
  cursor: pointer;
  transition: all .15s;
}
.btn-rechazar-fila:hover { background: #fef2f2; color: #dc2626; border-color: #fecaca; }
</style>
</head>
<body>

<div id="float-bar">
  <span class="float-n" id="float-n"></span>
  <button class="fb-verde" id="float-confirmar" style="display:none" onclick="floatConfirmar()">✓ Confirmar</button>
  <button class="fb-rojo"  id="float-borrar"    style="display:none" onclick="floatBorrar()">✕ Borrar</button>
  <button class="fb-cancel" onclick="floatCancelar()">✕ cancelar</button>
</div>

<div id="lightbox" onclick="cerrarLightbox()">
  <img id="lightbox-img" src="">
</div>

<header>
  <h1><span class="acc">Gastos</span><span class="sec"> OWA</span></h1>
  <div class="tabs">
    <div class="tab active" onclick="irTab('cola')" id="tab-cola">
      En cola <span class="badge" id="badge-cola">0</span>
    </div>
    <div class="tab" onclick="irTab('confirmar')" id="tab-confirmar">
      Calibrar <span class="badge" id="badge-confirmar">0</span>
    </div>
    <div class="tab" onclick="irTab('confirmados')" id="tab-confirmados">
      Boletas por Confirmar <span class="badge" id="badge-confirmados">0</span>
    </div>
    <div class="tab" onclick="irTab('validados')" id="tab-validados">
      Gastos Confirmados <span class="badge" id="badge-validados">0</span>
    </div>
    <div class="tab" onclick="irTab('api')" id="tab-api">Otros</div>
  </div>
  <div class="hdr-actions">
    <span id="status"></span>
    <button class="hbtn" onclick="recargar()">↺ Actualizar</button>
  </div>
</header>

<!-- TAB: Calibrar (IA ya clasificó, usuario calibra si es recibo o no) -->
<div class="page" id="page-confirmar">

  <div class="seccion" id="sec-recibos" style="display:none">
    <div class="sec-hdr">
      <h2 style="color:#15803d">✓ Son recibos</h2>
      <span class="n" id="n-recibos"></span>
      <button class="btn-sel" onclick="selTodos('recibos')">☐ Seleccionar todos</button>
      <button class="btn-verde" id="btn-confirmar-sel" style="display:none" onclick="confirmarSeleccionados()">✓ Confirmar seleccionados (0)</button>
      <button class="btn-verde" onclick="confirmarTodos()">Confirmar todos</button>
    </div>
    <div class="grid" id="grid-recibos"></div>
  </div>

  <div class="seccion" id="sec-no-recibos" style="display:none">
    <div class="sec-hdr">
      <h2 style="color:#dc2626">✕ No son recibos</h2>
      <span class="n" id="n-no-recibos"></span>
      <button class="btn-sel" onclick="selTodos('no-recibos')">☐ Seleccionar todos</button>
      <button class="btn-rojo" id="btn-borrar-sel" style="display:none" onclick="borrarSeleccionados()">✕ Borrar seleccionados (0)</button>
      <button class="btn-rojo" onclick="borrarTodos()">Borrar todos</button>
    </div>
    <div class="grid" id="grid-no-recibos"></div>
  </div>

  <div id="empty-confirmar" style="text-align:center;padding:50px 20px;color:#aaa;font-size:14px;display:none">
    <span id="empty-confirmar-txt">Cargando...</span>
  </div>
</div>

<!-- TAB: En cola (IA no ha analizado aún) -->
<div class="page active" id="page-cola">
  <div class="seccion" id="sec-sin-procesar" style="display:none">
    <div class="sec-hdr">
      <h2 style="color:#888">⏳ En cola <span id="lbl-analizando" style="font-weight:400;color:#aaa;font-size:12px;text-transform:none;letter-spacing:0"></span></h2>
      <span class="n" id="n-sin-procesar"></span>
      <button class="btn-sel" onclick="selTodos('en-cola')">☐ Seleccionar todos</button>
      <button class="btn-verde" id="btn-confirmar-cola-sel" style="display:none" onclick="confirmarSelCola()">✓ Confirmar seleccionados (0)</button>
      <button class="btn-rojo"  id="btn-borrar-cola-sel"    style="display:none" onclick="borrarSelCola()">✕ Borrar seleccionados (0)</button>
    </div>
    <div class="grid" id="grid-en-cola"></div>
  </div>
  <div id="empty-cola" style="text-align:center;padding:50px 20px;color:#aaa;font-size:14px;display:none">
    No hay fotos en cola ✓
  </div>
</div>

<!-- TAB: Boletas por Confirmar (tabla de revisión) -->
<div class="page" id="page-confirmados">
  <div class="seccion">
    <!-- Barra búsqueda + filtro + contador -->
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px;background:#f7f7f9;border:1px solid #e8e8ed;border-radius:8px;padding:5px 10px;flex:1;min-width:160px;max-width:360px">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#bbb" stroke-width="2.2" stroke-linecap="round" style="flex-shrink:0"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="22" y2="22"/></svg>
        <input id="buscar-recibos" type="text" placeholder="Buscar..."
          oninput="renderTablaConfirmados()"
          style="flex:1;border:none;background:transparent;font-size:12px;color:#111118;outline:none;min-width:0">
      </div>
      <select id="filtro-cat-tabla" onchange="renderTablaConfirmados()"
        style="padding:5px 10px;border-radius:7px;border:1px solid #e8e8ed;font-size:12px;color:#666;background:#fff">
        <option value="">Todas las categorías</option>
      </select>
      <span id="n-confirmados" style="font-size:12px;color:#bbb;white-space:nowrap"></span>
    </div>

    <!-- Bulk action bar (visible al seleccionar) -->
    <div id="bulk-bar" style="display:none;align-items:center;gap:10px;background:#f7f8fa;border:1px solid #e0e0e8;border-radius:10px;padding:8px 14px;margin-bottom:10px;flex-wrap:wrap">
      <span id="bulk-count" style="font-size:12px;font-weight:600;color:#555">0 seleccionados</span>
      <span style="color:#ddd;font-size:11px">|</span>
      <span style="font-size:12px;color:#999">Categoría:</span>
      <div id="bulk-cats" style="display:flex;gap:6px;flex-wrap:wrap"></div>
      <span style="color:#ddd;font-size:11px">|</span>
      <span style="font-size:12px;color:#999">Comercio:</span>
      <div style="display:flex;gap:6px;align-items:center">
        <input id="bulk-comercio" type="text" placeholder="Nombre del comercio..."
          style="font-size:12px;padding:4px 9px;border:1.5px solid #e0e0e8;border-radius:7px;outline:none;min-width:160px;background:#fff"
          onfocus="this.style.borderColor='#6366f1'" onblur="this.style.borderColor='#e0e0e8'"
          onkeydown="if(event.key==='Enter')cambiarComercioLote()">
        <button onclick="cambiarComercioLote()"
          style="padding:4px 10px;border-radius:7px;background:#fff;color:#6366f1;border:1.5px solid #c7d2fe;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap"
          onmouseenter="this.style.background='#eef2ff'" onmouseleave="this.style.background='#fff'">Aplicar</button>
      </div>
      <span style="color:#ddd;font-size:11px">|</span>
      <button onclick="validarLote('confirmar')"
        style="padding:5px 14px;border-radius:7px;background:#5b6bf5;color:#fff;border:none;font-size:12px;font-weight:600;cursor:pointer">✓ Confirmar</button>
      <button onclick="validarLote('rechazar')"
        style="padding:5px 14px;border-radius:7px;background:#fff;color:#999;border:1px solid #ddd;font-size:12px;font-weight:500;cursor:pointer">✗ Rechazar</button>
      <button onclick="limpiarSeleccion()" style="font-size:11px;padding:3px 9px;border-radius:6px;border:1px solid #e0e0e8;color:#aaa;background:#fff;cursor:pointer">Cancelar</button>
    </div>

    <!-- Tabla -->
    <div style="overflow-x:auto;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <table id="tabla-confirmados" style="width:100%;border-collapse:collapse;background:#fff;font-size:13px">
        <thead>
          <tr style="background:#f7f7f9;border-bottom:1px solid #e8e8ed">
            <th style="padding:10px 12px;width:36px">
              <input type="checkbox" id="chk-all" onchange="toggleTodos(this.checked)"
                style="width:15px;height:15px;cursor:pointer;accent-color:#6366f1">
            </th>
            <th style="padding:10px 8px;width:52px"></th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Comercio</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Fecha</th>
            <th style="padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Monto</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Categoría</th>
            <th style="padding:10px 12px;text-align:center;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap">Acción</th>
          </tr>
        </thead>
        <tbody id="tbody-confirmados"></tbody>
      </table>
    </div>
    <div id="empty-confirmados" style="text-align:center;padding:50px 20px;color:#aaa;font-size:14px;display:none">
      No hay recibos pendientes de confirmación
    </div>
  </div>
</div>

<!-- TAB: Gastos Confirmados (validados) -->
<div class="page" id="page-validados">
  <div class="seccion">
    <div class="sec-hdr">
      <h2>Gastos Confirmados</h2>
      <span class="n" id="n-validados"></span>
    </div>
    <div style="display:flex;align-items:center;gap:6px;background:#f7f7f9;border:1px solid #e8e8ed;border-radius:8px;padding:5px 10px;margin-bottom:12px;max-width:400px">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#bbb" stroke-width="2.2" stroke-linecap="round" style="flex-shrink:0"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="22" y2="22"/></svg>
      <input id="buscar-validados" type="text" placeholder="Buscar..."
        oninput="renderTablaValidados()"
        style="flex:1;border:none;background:transparent;font-size:12px;color:#111118;outline:none;min-width:0">
    </div>
    <div style="overflow-x:auto;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <table id="tabla-validados" style="width:100%;border-collapse:collapse;background:#fff;font-size:13px">
        <thead>
          <tr style="background:#f7f7f9;border-bottom:1px solid #e8e8ed">
            <th style="padding:10px 8px;width:52px"></th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Comercio</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Fecha</th>
            <th style="padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Monto</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px">Categoría</th>
          </tr>
        </thead>
        <tbody id="tbody-validados"></tbody>
      </table>
    </div>
    <div id="empty-validados" style="text-align:center;padding:50px 20px;color:#aaa;font-size:14px;display:none">
      Aún no hay gastos confirmados
    </div>
  </div>
</div>

<!-- TAB: API Keys -->
<div class="page" id="page-api">
  <div style="max-width:500px;margin:40px auto;padding:0 20px">
    <h2 style="color:#1a1a2e;margin-bottom:24px">🔑 Estado API Keys</h2>
    <div id="api-cards" style="display:flex;flex-direction:column;gap:16px">
      <!-- generado por JS -->
    </div>
    <div id="api-mes" style="text-align:center;color:#aaa;font-size:12px;margin-top:20px"></div>
  </div>
</div>

<script>
let nRec=0, nNoRec=0, nSinProc=0;
const selRec   = new Set();
const selNoRec = new Set();
const selCola  = new Set();

// Último card clickeado por sección (para Shift+click)
const lastClick = {};

function pathId(path) {
  return btoa(unescape(encodeURIComponent(path)));
}

function _setForSec(seccion) {
  if (seccion === 'recibos')    return selRec;
  if (seccion === 'no-recibos') return selNoRec;
  return selCola;
}

function _gridForSec(seccion) {
  if (seccion === 'recibos')    return document.getElementById('grid-recibos');
  if (seccion === 'no-recibos') return document.getElementById('grid-no-recibos');
  return document.getElementById('grid-en-cola');
}

function toggleSel(path, seccion, shiftKey) {
  const s    = _setForSec(seccion);
  const card = document.getElementById('card-' + pathId(path));
  if (!card) return;

  if (shiftKey && lastClick[seccion]) {
    // Seleccionar rango entre lastClick y este
    const grid  = _gridForSec(seccion);
    const cards = [...grid.querySelectorAll('.card')];
    const iA    = cards.findIndex(c => c.dataset.path === lastClick[seccion]);
    const iB    = cards.findIndex(c => c.dataset.path === path);
    if (iA !== -1 && iB !== -1) {
      const [from, to] = iA < iB ? [iA, iB] : [iB, iA];
      for (let i = from; i <= to; i++) {
        const p = cards[i].dataset.path;
        s.add(p);
        cards[i].classList.add('sel');
        cards[i].querySelector('.chk').textContent = '✓';
      }
      actualizarBotonesSel();
      return;
    }
  }

  if (s.has(path)) {
    s.delete(path); card.classList.remove('sel'); card.querySelector('.chk').textContent = '';
  } else {
    s.add(path); card.classList.add('sel'); card.querySelector('.chk').textContent = '✓';
  }
  lastClick[seccion] = path;
  actualizarBotonesSel();
}

function selTodos(seccion) {
  const s     = _setForSec(seccion);
  const gridId = seccion === 'en-cola' ? 'grid-en-cola' : 'grid-' + seccion;
  const cards = document.getElementById(gridId).querySelectorAll('.card');
  const todos = [...cards].every(c => c.classList.contains('sel'));
  cards.forEach(card => {
    const path = card.dataset.path;
    if (todos) { s.delete(path); card.classList.remove('sel'); card.querySelector('.chk').textContent = ''; }
    else        { s.add(path);   card.classList.add('sel');    card.querySelector('.chk').textContent = '✓'; }
  });
  actualizarBotonesSel();
}

function actualizarBotonesSel() {
  const btnR  = document.getElementById('btn-confirmar-sel');
  const btnN  = document.getElementById('btn-borrar-sel');
  const btnCR = document.getElementById('btn-confirmar-cola-sel');
  const btnCN = document.getElementById('btn-borrar-cola-sel');
  if (selRec.size > 0)    { btnR.style.display='';  btnR.textContent=`✓ Confirmar seleccionados (${selRec.size})`; }
  else                     { btnR.style.display='none'; }
  if (selNoRec.size > 0)  { btnN.style.display='';  btnN.textContent=`✕ Borrar seleccionados (${selNoRec.size})`; }
  else                     { btnN.style.display='none'; }
  if (selCola.size > 0)   { btnCR.style.display=''; btnCR.textContent=`✓ Confirmar seleccionados (${selCola.size})`;
                             btnCN.style.display=''; btnCN.textContent=`✕ Borrar seleccionados (${selCola.size})`; }
  else                     { btnCR.style.display='none'; btnCN.style.display='none'; }

  // Barra flotante
  const total = selRec.size + selNoRec.size + selCola.size;
  const bar   = document.getElementById('float-bar');
  const fc    = document.getElementById('float-confirmar');
  const fb    = document.getElementById('float-borrar');
  if (total > 0) {
    bar.classList.add('visible');
    document.getElementById('float-n').textContent = total + ' seleccionada' + (total>1?'s':'');
    // Mostrar confirmar solo si hay recibos o cola seleccionados
    fc.style.display = (selRec.size > 0 || selCola.size > 0) ? '' : 'none';
    // Mostrar borrar siempre que haya algo
    fb.style.display = '';
  } else {
    bar.classList.remove('visible');
  }
}

function floatConfirmar() {
  if (selRec.size > 0)   confirmarSeleccionados();
  if (selCola.size > 0)  confirmarSelCola();
}
async function floatBorrar() {
  // Juntar todos los paths seleccionados de cualquier sección y borrar en batch
  const paths = [...selRec, ...selNoRec, ...selCola];
  selRec.clear(); selNoRec.clear(); selCola.clear(); actualizarBotonesSel();
  paths.forEach(p => { const c = document.getElementById('card-'+pathId(p)); if(c) c.remove(); });
  nRec = Math.max(0, document.getElementById('grid-recibos').querySelectorAll('.card').length);
  nNoRec = Math.max(0, document.getElementById('grid-no-recibos').querySelectorAll('.card').length);
  nSinProc = Math.max(0, document.getElementById('grid-en-cola').querySelectorAll('.card').length);
  actualizarVista();
  await fetch('/api/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}
function floatCancelar() {
  // Deseleccionar todo
  ['recibos','no-recibos','en-cola'].forEach(sec => {
    const s = _setForSec(sec);
    const gridId = sec === 'en-cola' ? 'grid-en-cola' : 'grid-' + sec;
    document.getElementById(gridId).querySelectorAll('.card.sel').forEach(c => {
      c.classList.remove('sel'); c.querySelector('.chk').textContent = '';
    });
    s.clear();
  });
  actualizarBotonesSel();
}

async function confirmarSeleccionados() {
  const paths = [...selRec]; selRec.clear(); actualizarBotonesSel();
  paths.forEach(p => { const c = document.getElementById('card-'+pathId(p)); if(c) { c.remove(); nRec=Math.max(0,nRec-1); } });
  actualizarVista();
  const badge = document.getElementById('badge-confirmados');
  badge.textContent = parseInt(badge.textContent||'0') + paths.length;
  await fetch('/api/confirm-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}
async function borrarSeleccionados() {
  const paths = [...selNoRec]; selNoRec.clear(); actualizarBotonesSel();
  paths.forEach(p => { const c = document.getElementById('card-'+pathId(p)); if(c) { c.remove(); nNoRec=Math.max(0,nNoRec-1); } });
  actualizarVista();
  await fetch('/api/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}
async function confirmarSelCola() {
  const paths = [...selCola]; selCola.clear(); actualizarBotonesSel();
  paths.forEach(p => { const c = document.getElementById('card-'+pathId(p)); if(c) { c.remove(); nSinProc=Math.max(0,nSinProc-1); } });
  actualizarVista();
  const badge = document.getElementById('badge-confirmados');
  badge.textContent = parseInt(badge.textContent||'0') + paths.length;
  await fetch('/api/confirm-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}
async function borrarSelCola() {
  const paths = [...selCola]; selCola.clear(); actualizarBotonesSel();
  paths.forEach(p => { const c = document.getElementById('card-'+pathId(p)); if(c) { c.remove(); nSinProc=Math.max(0,nSinProc-1); } });
  actualizarVista();
  await fetch('/api/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}

function setStatus(txt) { document.getElementById('status').textContent = txt; }

function actualizarVista() {
  const nConfirmar = nRec + nNoRec;
  document.getElementById('badge-confirmar').textContent = nConfirmar;
  document.getElementById('badge-cola').textContent      = nSinProc;
  document.getElementById('sec-recibos').style.display      = nRec   > 0 ? '' : 'none';
  document.getElementById('sec-no-recibos').style.display   = nNoRec > 0 ? '' : 'none';
  document.getElementById('sec-sin-procesar').style.display = nSinProc > 0 ? '' : 'none';
  document.getElementById('n-recibos').textContent      = nRec     + ' foto' + (nRec>1?'s':'');
  document.getElementById('n-no-recibos').textContent   = nNoRec   + ' foto' + (nNoRec>1?'s':'');
  document.getElementById('n-sin-procesar').textContent = nSinProc + ' foto' + (nSinProc>1?'s':'');
  // empty-confirmar solo se muestra si ya terminó de cargar (gestionado por cargar())

  document.getElementById('empty-cola').style.display      = nSinProc  === 0 ? '' : 'none';
  if (nSinProc > 0) setStatus('Analizando... ' + nSinProc + ' en cola');
  else if (nConfirmar > 0) setStatus(nConfirmar + ' para confirmar');
  else setStatus('Todo listo ✓');
}

function irTab(tab) {
  document.querySelectorAll('.tab,.page').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.getElementById('page-' + tab).classList.add('active');
  if (tab === 'confirmados') cargarConfirmados();
  if (tab === 'validados')  cargarValidados();
  if (tab === 'api') cargarApiStats();
}

function cargarApiStats() {
  fetch('/api/stats').then(r => r.json()).then(d => {
    const lim    = d.mistral_limite || 2000;
    const activa = d.mistral_cuenta || 'owa605';
    const agotada1 = activa === 'owa605.g66';
    const keys = [
      { nombre: 'owa605',      usado: d.mistral_c1 || 0, agotada: agotada1,  esActiva: !agotada1 },
      { nombre: 'owa605.g66',  usado: d.mistral_c2 || 0, agotada: false,      esActiva: agotada1  },
    ];
    const container = document.getElementById('api-cards');
    container.innerHTML = keys.map(k => {
      const pct    = Math.round(k.usado / lim * 100);
      const libre  = lim - k.usado;
      let color, etiq, barColor;
      if (k.agotada)       { color='#ef4444'; etiq='AGOTADA';  barColor='#ef4444'; }
      else if (k.esActiva) { color='#16a34a'; etiq='ACTIVA';   barColor='#16a34a'; }
      else                 { color='#9ca3af'; etiq='STANDBY';  barColor='#9ca3af'; }
      const barPct = Math.min(pct, 100);
      return `<div style="background:#fff;border:1px solid #ebebef;border-radius:12px;padding:20px 24px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <span style="font-weight:700;font-size:15px;color:#1a1a2e">${k.nombre}</span>
          <span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;
                background:${color}18;color:${color};border:1px solid ${color}40">${etiq}</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:13px;color:#555;margin-bottom:8px">
          <span><b style="color:#1a1a2e">${k.usado.toLocaleString()}</b> usadas</span>
          <span><b style="color:#1a1a2e">${libre.toLocaleString()}</b> disponibles</span>
          <span><b style="color:${color}">${pct}%</b></span>
        </div>
        <div style="background:#f3f4f6;border-radius:6px;height:8px;overflow:hidden">
          <div style="background:${barColor};width:${barPct}%;height:100%;border-radius:6px;transition:width .4s"></div>
        </div>
        <div style="text-align:right;font-size:11px;color:#aaa;margin-top:6px">${k.usado} / ${lim}</div>
      </div>`;
    }).join('');
    document.getElementById('api-mes').textContent = d.mes || '';
  });
}

async function comprimirRetroactivo() {
  const btn = document.getElementById('btn-retro');
  const msg = document.getElementById('retro-msg');
  btn.disabled = true;
  btn.textContent = 'Iniciando...';
  const r = await fetch('/api/comprimir-retroactivo', {method:'POST'});
  const d = await r.json();
  btn.textContent = 'Corriendo en background';
  msg.textContent = 'Ver progreso en el terminal — puede tardar varios minutos.';
}

function crearCard(path, det, tipo) {
  const id   = pathId(path);
  const card = document.createElement('div');
  const sec  = tipo === 'recibo' ? 'recibos' : tipo === 'no-recibo' ? 'no-recibos' : tipo === 'sin-procesar' ? 'en-cola' : '';
  card.className    = 'card';
  card.id           = 'card-' + id;
  card.dataset.path = path;
  if (sec) card.onclick = (e) => { if (!e.target.closest('button') && !e.target.closest('img')) toggleSel(path, sec, e.shiftKey); };
  const thumbUrl = '/thumb?path=' + encodeURIComponent(path);
  const imgUrl   = '/img?path='   + encodeURIComponent(path);
  const imgClick = `event.stopPropagation(); abrirLightbox('${imgUrl}')`;
  let acts = '';
  if (tipo === 'recibo') {
    acts = `<button class="cv" onclick="confirmar(this)">✓ Confirmar</button>
            <button class="cr" onclick="borrar(this)">✕ Borrar</button>`;
  } else if (tipo === 'no-recibo') {
    acts = `<button class="cr" onclick="borrar(this)">✕ Borrar</button>
            <button class="cv" onclick="confirmar(this)">✓ Es recibo</button>`;
  } else if (tipo === 'confirmado') {
    acts = `<button class="cr" onclick="borrar(this)" title="Eliminar de recibos" style="flex:none;padding:4px 10px">🗑</button>`;
  } else {
    acts = `<button class="cv" onclick="confirmar(this)">✓ Recibo</button>
            <button class="cr" onclick="borrar(this)">✕ Borrar</button>`;
  }
  card.innerHTML = `
    ${sec ? '<div class="chk"></div>' : ''}
    <img src="${thumbUrl}" loading="lazy" onerror="this.src='${imgUrl}'"
         onclick="${imgClick}" style="cursor:zoom-in">
    <div class="info">
      <div class="det">${det}</div>
      <div class="acts">${acts}</div>
    </div>`;
  return card;
}

function moverCard(path, esRecibo, d) {
  const card = document.getElementById('card-' + pathId(path));
  if (!card) return;
  card.remove();
  selCola.delete(path);
  nSinProc = Math.max(0, nSinProc - 1);
  const det = (d && d.proveedor) ? d.proveedor : path.split('/').pop();
  if (esRecibo) {
    nRec++;
    document.getElementById('grid-recibos').appendChild(crearCard(path, det, 'recibo'));
  } else {
    nNoRec++;
    document.getElementById('grid-no-recibos').appendChild(crearCard(path, det, 'no-recibo'));
  }
  actualizarVista();
}

async function cargar() {
  setStatus('Cargando...');
  ['grid-recibos','grid-no-recibos','grid-en-cola'].forEach(id =>
    document.getElementById(id).innerHTML = '');
  nRec = nNoRec = nSinProc = 0;
  selRec.clear(); selNoRec.clear(); selCola.clear(); actualizarBotonesSel();
  // Ocultar empty-confirmar mientras carga
  document.getElementById('empty-confirmar').style.display = 'none';

  const res  = await fetch('/api/photos');
  const data = await res.json();
  if (!data.photos || data.photos.length === 0) {
    document.getElementById('empty-confirmar-txt').textContent = 'La IA aún no ha clasificado fotos — espera un momento ⏳';
    actualizarVista(); return;
  }

  const pendientes = [];

  data.photos.forEach(p => {
    if (p.estado === 'recibo') {
      nRec++;
      const det = p.proveedor || p.name;
      document.getElementById('grid-recibos').appendChild(crearCard(p.path, det, 'recibo'));
    } else if (p.estado === 'no_recibo') {
      nNoRec++;
      document.getElementById('grid-no-recibos').appendChild(crearCard(p.path, p.name, 'no-recibo'));
    } else {
      nSinProc++;
      document.getElementById('grid-en-cola').appendChild(crearCard(p.path, p.name, 'sin-procesar'));
      pendientes.push(p);
    }
  });
  actualizarVista();

  // Clasificar solo los pendientes (los demás ya están en caché)
  let procesados = 0;
  const totalPend = pendientes.length;
  if (totalPend > 0) {
    (async () => {
      for (const p of pendientes) {
        if (!document.getElementById('card-' + pathId(p.path))) {
          nSinProc = Math.max(0, nSinProc - 1);
          procesados++;
          actualizarVista();
          continue;
        }
        // Marcar visualmente cuál se está analizando ahora
        const lbl = document.getElementById('lbl-analizando');
        if (lbl) lbl.textContent = `— analizando ${procesados+1}/${totalPend}`;
        try {
          const r = await fetch('/api/classify', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({path: p.path})
          });
          const d = await r.json();
          if (!document.getElementById('card-' + pathId(p.path))) { procesados++; continue; }
          moverCard(p.path, d.es_recibo, d);
        } catch(e) {
          nSinProc = Math.max(0, nSinProc - 1);
          actualizarVista();
        }
        procesados++;
        await new Promise(r => setTimeout(r, 1500));
      }
      const lbl = document.getElementById('lbl-analizando');
      if (lbl) lbl.textContent = '';
    })();
  }
}

// ── Categorías ────────────────────────────────────────────────────────────────
const CATEGORIAS = ["Viajes","Representación","Gastos Varios de Oficina","Servicios Profesionales","Cuentas","Otros"];
const MONEDA_FLAG = {"CLP":"🇨🇱","USD":"🇺🇸","PEN":"🇵🇪","BRL":"🇧🇷"};
const MESES_ES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"];
function fmtFecha(f) {
  if (!f) return '—';
  const p = f.split('-');
  if (p.length !== 3) return f;
  return parseInt(p[2]) + ' ' + MESES_ES[parseInt(p[1])-1] + ' ' + p[0];
}
const CAT_COLOR  = {
  "Viajes":                    "#3b82f6",
  "Representación":            "#f59e0b",
  "Gastos Varios de Oficina":  "#10b981",
  "Servicios Profesionales":   "#8b5cf6",
  "Cuentas":                   "#ef4444",
  "Otros":                     "#6b7280",
};

function catBadge(cat) {
  const c = CAT_COLOR[cat] || "#9ca3af";
  return '<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:500;color:#555;white-space:nowrap">'
    + '<span style="width:7px;height:7px;border-radius:50%;background:' + c + ';flex-shrink:0;display:inline-block"></span>'
    + (cat||'Otros')
    + '</span>';
}

// ── Cards confirmados ─────────────────────────────────────────────────────────
let _todosConfirmados = [];
let _filtroCategoria  = '';
let _modoSeleccion    = false;
let _seleccionados    = new Set(); // names seleccionados

function imgFallback(img) {
  img.onerror = null;
  var fb = img.dataset.fallback;
  if (fb && img.src !== fb) img.src = fb;
}

function crearCardConfirmado(p) {
  const card = document.createElement('div');
  card.style.cssText = 'background:#fff;border:1px solid #ebebef;border-radius:12px;overflow:hidden;display:flex;flex-direction:column;cursor:pointer;transition:box-shadow .15s';
  card.onmouseenter = function() { card.style.boxShadow = '0 4px 16px rgba(0,0,0,.10)'; };
  card.onmouseleave = function() { card.style.boxShadow = ''; };
  card.dataset.name = p.name;
  card.onclick = function() {
    if (_modoSeleccion) { toggleSeleccion(p.name); }
    else { abrirModalEdicion(p); }
  };
  const thumbUrl = '/thumb?path=' + encodeURIComponent(p.path);
  const imgUrl   = '/img?path='   + encodeURIComponent(p.path);
  const prov     = p.proveedor && p.proveedor !== '[ilegible]' && p.proveedor !== '[sin_archivo]' ? p.proveedor : null;
  const incompleta = !prov || p.monto == null;
  const mon = p.moneda || 'CLP';
  const flag = MONEDA_FLAG[mon] || '';
  const montoStr = p.monto != null ? (mon !== 'CLP' && flag ? flag + ' ' : '') + Number(p.monto).toLocaleString('es-CL') : '—';
  const montoStyle = mon !== 'CLP' ? 'font-weight:700;color:#4f46e5' : 'font-weight:600';
  const fecha    = p.fecha || '—';
  const cat      = p.categoria || 'Otros';
  if (incompleta) {
    card.style.borderLeft = '3px solid #f97316';
  }
  const sel = _seleccionados.has(p.name);
  if (sel) { card.style.outline = '2.5px solid #6366f1'; card.style.background = '#f0f0ff'; }
  card.innerHTML =
    '<div style="position:relative">' +
      '<img src="' + thumbUrl + '" data-fallback="' + imgUrl + '" loading="lazy" style="width:100%;aspect-ratio:4/3;object-fit:cover;display:block">' +
      '<div style="position:absolute;top:6px;left:6px">' + catBadge(cat) + '</div>' +
      (incompleta ? '<div style="position:absolute;top:6px;right:6px;font-size:13px;line-height:1;background:rgba(255,255,255,.9);border-radius:4px;padding:2px 4px">⚠</div>' : '') +
      (!incompleta && p.editado ? '<div style="position:absolute;top:6px;right:6px;font-size:10px;background:rgba(255,255,255,.85);border-radius:4px;padding:2px 5px;color:#555">editado</div>' : '') +
      (_modoSeleccion ? '<div class="sel-chk" style="position:absolute;bottom:6px;right:6px;width:20px;height:20px;border-radius:50%;border:2px solid #6366f1;background:' + (sel?'#6366f1':'rgba(255,255,255,.9)') + ';display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;font-weight:700">' + (sel?'✓':'') + '</div>' : '') +
    '</div>' +
    '<div style="padding:9px 11px;flex:1;display:flex;flex-direction:column;gap:3px">' +
      '<div style="font-size:12px;font-weight:700;color:#1a1a2e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
        (prov || '<span style="color:#bbb;font-weight:400">Sin datos</span>') +
      '</div>' +
      '<div style="display:flex;justify-content:space-between;font-size:11px;color:#555">' +
        '<span style="' + montoStyle + '">' + montoStr + '</span>' +
        '<span style="color:#aaa">' + fecha + '</span>' +
      '</div>' +
    '</div>';
  var img = card.querySelector('img');
  if (img) img.onerror = function() { imgFallback(this); };
  return card;
}

async function cargarConfirmados() {
  document.getElementById('tbody-confirmados').innerHTML =
    '<tr><td colspan="7" style="padding:20px;color:#aaa;text-align:center;font-size:13px">Cargando...</td></tr>';
  document.getElementById('n-confirmados').textContent = '';
  document.getElementById('empty-confirmados').style.display = 'none';
  const res  = await fetch('/api/recibos-confirmados');
  const data = await res.json();
  _todosConfirmados = data.photos || [];
  _seleccionados.clear();
  _poblarFiltroCat();
  renderTablaConfirmados();
}

function _poblarFiltroCat() {
  const sel = document.getElementById('filtro-cat-tabla');
  if (!sel) return;
  const counts = {};
  _todosConfirmados.forEach(p => { const c = p.categoria||'Otros'; counts[c]=(counts[c]||0)+1; });
  sel.innerHTML = '<option value="">Todas las categorías</option>';
  CATEGORIAS.forEach(cat => {
    if (!counts[cat]) return;
    const o = document.createElement('option'); o.value = cat;
    o.textContent = cat + ' (' + counts[cat] + ')'; sel.appendChild(o);
  });
  // También poblar bulk cats
  const bc = document.getElementById('bulk-cats');
  if (bc) {
    bc.innerHTML = '';
    CATEGORIAS.forEach(cat => {
      const b = document.createElement('button');
      b.textContent = cat;
      b.style.cssText = 'font-size:10px;padding:2px 9px;border-radius:20px;border:1.5px solid ' + (CAT_COLOR[cat]||'#9ca3af') + ';color:' + (CAT_COLOR[cat]||'#9ca3af') + ';background:#fff;cursor:pointer';
      b.onclick = function() { cambiarCatLote(cat); };
      bc.appendChild(b);
    });
  }
}

function renderTablaConfirmados() {
  const q   = (document.getElementById('buscar-recibos')||{value:''}).value.trim().toLowerCase();
  const cat = (document.getElementById('filtro-cat-tabla')||{value:''}).value;
  const filtrados = _todosConfirmados.filter(p => {
    if (cat && (p.categoria||'Otros') !== cat) return false;
    if (q) {
      const texto = [(p.proveedor||''), (p.fecha||''), (p.monto||''), (p.categoria||''), (p.moneda||'')].join(' ').toLowerCase();
      if (!texto.includes(q)) return false;
    }
    return true;
  });

  const n = _todosConfirmados.length;
  document.getElementById('badge-confirmados').textContent = n;
  document.getElementById('n-confirmados').textContent = n + ' recibo' + (n!==1?'s':'') + (filtrados.length!==n ? ' · ' + filtrados.length + ' mostrados' : '');
  const tbody = document.getElementById('tbody-confirmados');
  tbody.innerHTML = '';

  if (!filtrados.length) {
    document.getElementById('empty-confirmados').style.display = '';
    return;
  }
  document.getElementById('empty-confirmados').style.display = 'none';

  filtrados.forEach(p => {
    const prov    = p.proveedor && !['[ilegible]','[sin_archivo]'].includes(p.proveedor) ? p.proveedor : null;
    const mon     = p.moneda || 'CLP';
    const flag    = MONEDA_FLAG[mon] || '';
    const montoTxt= p.monto != null ? (mon !== 'CLP' && flag ? flag + ' ' : '') + Number(p.monto).toLocaleString('es-CL') : '—';
    const cat     = p.categoria || 'Otros';
    const color   = CAT_COLOR[cat] || '#9ca3af';
    const incompleto = !prov || p.monto == null;
    const sel     = _seleccionados.has(p.name);
    const thumbUrl= '/thumb?path=' + encodeURIComponent(p.path);

    const tr = document.createElement('tr');
    tr.dataset.name = p.name;
    tr.style.cssText = 'border-bottom:1px solid #f0f0f4;transition:background .1s;cursor:pointer;' + (sel ? 'background:#f0f0fe;' : '');
    tr.onmouseenter = function() { if (!_seleccionados.has(p.name)) this.style.background='#f7f7fb'; };
    tr.onmouseleave = function() { this.style.background = _seleccionados.has(p.name) ? '#f0f0fe' : ''; };

    tr.innerHTML =
      '<td style="padding:8px 12px"><input type="checkbox" class="fila-chk" ' + (sel?'checked':'') + ' style="width:14px;height:14px;cursor:pointer;accent-color:#4f46e5"></td>' +
      '<td style="padding:5px 8px"><img class="fila-thumb" src="' + thumbUrl + '" style="width:42px;height:42px;border-radius:6px;object-fit:cover;display:block"></td>' +
      '<td style="padding:8px 12px">' +
        (prov ? '<span style="font-weight:500;color:#111118">' + prov + '</span>'
              : '<span style="color:#ccc;font-style:italic">Sin datos</span>') +
        (incompleto ? ' <span style="font-size:10px;background:#fff7ed;color:#c2410c;border:1px solid #fdba74;border-radius:4px;padding:1px 5px">⚠</span>' : '') +
      '</td>' +
      '<td style="padding:8px 12px;color:#aaa;font-size:12px;white-space:nowrap">' + fmtFecha(p.fecha) + '</td>' +
      '<td style="padding:8px 12px;text-align:right;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums;' + (mon!=='CLP'?'color:#4f46e5':'color:#111118') + '">' + montoTxt + '</td>' +
      '<td style="padding:8px 12px"><span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#666"><span style="width:6px;height:6px;border-radius:50%;background:' + color + ';flex-shrink:0;display:inline-block"></span>' + cat + '</span></td>' +
      '<td style="padding:8px 12px;text-align:right;white-space:nowrap"><div style="display:inline-flex;gap:5px"><button class="btn-confirmar-fila">✓ Confirmar</button><button class="btn-rechazar-fila">✗</button></div></td>';

    // Wiring eventos sin escape de strings
    tr.querySelector('.fila-chk').addEventListener('change', function(e) { e.stopPropagation(); toggleFila(this, p.name); });
    tr.querySelector('.fila-thumb').onerror = function() { this.style.display = 'none'; };
    tr.querySelector('.btn-confirmar-fila').addEventListener('click', function(e) { e.stopPropagation(); validarUno(p.name, 'confirmar'); });
    tr.querySelector('.btn-rechazar-fila').addEventListener('click',  function(e) { e.stopPropagation(); validarUno(p.name, 'rechazar');  });
    tr.onclick = function() { abrirModalEdicion(p); };
    document.getElementById('tbody-confirmados').appendChild(tr);
  });
}

function toggleFila(chk, name) {
  if (chk.checked) _seleccionados.add(name);
  else _seleccionados.delete(name);
  const tr = document.querySelector('#tbody-confirmados tr[data-name="' + CSS.escape(name) + '"]');
  if (tr) tr.style.background = chk.checked ? '#f0f0fe' : '';
  _actualizarBulkBar();
}

function toggleTodos(checked) {
  const filtrados = document.querySelectorAll('#tbody-confirmados tr[data-name]');
  filtrados.forEach(tr => {
    const name = tr.dataset.name;
    if (checked) _seleccionados.add(name); else _seleccionados.delete(name);
    tr.style.background = checked ? '#f0f0fe' : '';
    const chk = tr.querySelector('input[type=checkbox]');
    if (chk) chk.checked = checked;
  });
  _actualizarBulkBar();
}

function limpiarSeleccion() {
  _seleccionados.clear();
  document.querySelectorAll('#tbody-confirmados input[type=checkbox]').forEach(c => c.checked = false);
  document.querySelectorAll('#tbody-confirmados tr[data-name]').forEach(tr => tr.style.background = '');
  document.getElementById('chk-all').checked = false;
  _actualizarBulkBar();
}

function _actualizarBulkBar() {
  const n = _seleccionados.size;
  const bar = document.getElementById('bulk-bar');
  if (bar) { bar.style.display = n > 0 ? 'flex' : 'none'; }
  const lbl = document.getElementById('bulk-count');
  if (lbl) lbl.textContent = n + ' seleccionado' + (n!==1?'s':'');
}

function cambiarCatLote(cat) {
  if (!_seleccionados.size) return;
  fetch('/api/editar-lote', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({names: Array.from(_seleccionados), categoria: cat})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      _seleccionados.forEach(name => {
        const p = _todosConfirmados.find(x => x.name === name);
        if (p) p.categoria = cat;
      });
      renderTablaConfirmados();
    }
  });
}

function cambiarComercioLote() {
  if (!_seleccionados.size) return;
  const input = document.getElementById('bulk-comercio');
  const proveedor = (input.value || '').trim();
  if (!proveedor) { input.focus(); return; }
  fetch('/api/editar-lote', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({names: Array.from(_seleccionados), proveedor})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      _seleccionados.forEach(name => {
        const p = _todosConfirmados.find(x => x.name === name);
        if (p) { p.proveedor = proveedor; p.editado = true; }
      });
      input.value = '';
      renderTablaConfirmados();
    }
  });
}

async function validarUno(name, accion) {
  const r = await fetch('/api/validar-gasto', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({names:[name], accion})
  });
  const d = await r.json();
  if (d.ok) {
    _todosConfirmados = _todosConfirmados.filter(p => p.name !== name);
    renderTablaConfirmados();
    if (accion === 'confirmar') cargarValidados();
  }
}

async function validarLote(accion) {
  if (!_seleccionados.size) return;
  const names = Array.from(_seleccionados);
  const r = await fetch('/api/validar-gasto', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({names, accion})
  });
  const d = await r.json();
  if (d.ok) {
    _todosConfirmados = _todosConfirmados.filter(p => !_seleccionados.has(p.name));
    _seleccionados.clear();
    renderTablaConfirmados();
    if (accion === 'confirmar') cargarValidados();
  }
}

// ── Gastos confirmados (validados) ────────────────────────────────────────────
let _todosValidados = [];

async function cargarValidados() {
  const res  = await fetch('/api/gastos-validados');
  const data = await res.json();
  _todosValidados = data.gastos || [];
  renderTablaValidados();
}

function renderTablaValidados() {
  const q = (document.getElementById('buscar-validados')||{value:''}).value.trim().toLowerCase();
  const filtrados = _todosValidados.filter(p => {
    if (!q) return true;
    return [(p.proveedor||''), (p.fecha||''), (p.monto||''), (p.categoria||'')].join(' ').toLowerCase().includes(q);
  });
  const n = _todosValidados.length;
  document.getElementById('badge-validados').textContent = n;
  document.getElementById('n-validados').textContent = n + ' gasto' + (n!==1?'s':'') + (filtrados.length!==n ? ' · ' + filtrados.length + ' mostrados' : '');
  const tbody = document.getElementById('tbody-validados');
  tbody.innerHTML = '';
  if (!filtrados.length) { document.getElementById('empty-validados').style.display = ''; return; }
  document.getElementById('empty-validados').style.display = 'none';
  filtrados.forEach(p => {
    const mon  = p.moneda || 'CLP';
    const flag = MONEDA_FLAG[mon] || '';
    const cat  = p.categoria || 'Otros';
    const color= CAT_COLOR[cat] || '#9ca3af';
    const tr   = document.createElement('tr');
    tr.style.cssText = 'border-bottom:1px solid #f0f0f4;cursor:pointer';
    tr.onmouseenter = function() { this.style.background='#f7f7fb'; };
    tr.onmouseleave = function() { this.style.background=''; };
    tr.onclick = function() { abrirModalEdicion(p); };
    tr.innerHTML =
      '<td style="padding:5px 8px"><img class="val-thumb" src="/thumb?path=' + encodeURIComponent(p.path) + '" style="width:40px;height:40px;border-radius:6px;object-fit:cover;display:block"></td>' +
      '<td style="padding:8px 12px;font-weight:500;color:#111118">' + (p.proveedor||'—') + '</td>' +
      '<td style="padding:8px 12px;color:#aaa;font-size:12px">' + fmtFecha(p.fecha) + '</td>' +
      '<td style="padding:8px 12px;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;' + (mon!=='CLP'?'color:#4f46e5':'color:#111118') + '">' + (p.monto!=null ? (mon!=='CLP'&&flag?flag+' ':'') + Number(p.monto).toLocaleString('es-CL') : '—') + '</td>' +
      '<td style="padding:8px 12px"><span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#666"><span style="width:6px;height:6px;border-radius:50%;background:' + color + ';flex-shrink:0;display:inline-block"></span>' + cat + '</span></td>';
    tr.querySelector('.val-thumb').onerror = function() { this.style.display = 'none'; };
    tbody.appendChild(tr);
  });
}

function renderFiltrosCat() { _poblarFiltroCat(); }
function _getFiltrados() { return _todosConfirmados; }
function renderConfirmados() { renderTablaConfirmados(); }

// ── Modo selección múltiple ────────────────────────────────────────────────────
function toggleModoSeleccion() {
  _modoSeleccion = !_modoSeleccion;
  _seleccionados.clear();
  const btn = document.getElementById('btn-seleccion');
  const toolbar = document.getElementById('toolbar-seleccion');
  if (_modoSeleccion) {
    btn.style.background = '#6366f1';
    btn.style.color = '#fff';
    toolbar.style.display = 'flex';
    renderCatsLote();
  } else {
    btn.style.background = '#fff';
    btn.style.color = '#6366f1';
    toolbar.style.display = 'none';
  }
  renderConfirmados();
}

function renderCatsLote() {
  const cont = document.getElementById('cats-lote');
  cont.innerHTML = '';
  CATEGORIAS.forEach(function(cat) {
    const b = document.createElement('button');
    b.textContent = cat;
    b.style.cssText = 'font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px;border:1.5px solid ' + CAT_COLOR[cat] + ';color:' + CAT_COLOR[cat] + ';background:#fff;cursor:pointer';
    b.onclick = function() { aplicarCategoriaLote(cat); };
    cont.appendChild(b);
  });
}

function toggleSeleccion(name) {
  if (_seleccionados.has(name)) _seleccionados.delete(name);
  else _seleccionados.add(name);
  // Actualizar visual de la card
  const card = document.querySelector('[data-name="' + CSS.escape(name) + '"]');
  if (card) actualizarCardSeleccion(card, _seleccionados.has(name));
  actualizarToolbarSeleccion();
}

function actualizarCardSeleccion(card, sel) {
  card.style.outline = sel ? '2.5px solid #6366f1' : '';
  card.style.background = sel ? '#f0f0ff' : '#fff';
  const chk = card.querySelector('.sel-chk');
  if (chk) chk.textContent = sel ? '✓' : '';
}

function actualizarToolbarSeleccion() {
  const lbl = document.getElementById('lbl-seleccion');
  if (lbl) lbl.textContent = _seleccionados.size + ' seleccionado' + (_seleccionados.size!==1?'s':'');
}

function seleccionarTodos() {
  _getFiltrados().forEach(function(p) { _seleccionados.add(p.name); });
  document.querySelectorAll('[data-name]').forEach(function(card) {
    if (_seleccionados.has(card.dataset.name)) actualizarCardSeleccion(card, true);
  });
  actualizarToolbarSeleccion();
}

function limpiarSeleccion() {
  _seleccionados.clear();
  document.querySelectorAll('[data-name]').forEach(function(card) {
    actualizarCardSeleccion(card, false);
  });
  actualizarToolbarSeleccion();
}

async function aplicarCategoriaLote(cat) {
  if (!_seleccionados.size) { alert('Selecciona al menos un recibo'); return; }
  const names = Array.from(_seleccionados);
  const r = await fetch('/api/editar-lote', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({names, categoria: cat})
  });
  const data = await r.json();
  if (data.ok) {
    // Actualizar local
    _todosConfirmados.forEach(function(p) {
      if (_seleccionados.has(p.name)) { p.categoria = cat; p.editado = true; }
    });
    _seleccionados.clear();
    renderFiltrosCat();
    renderConfirmados();
  }
}

function filtrarCategoria(cat) {
  _filtroCategoria = (_filtroCategoria === cat) ? '' : cat;
  document.querySelectorAll('.cat-filter-btn').forEach(function(b) {
    const sel = b.dataset.cat === _filtroCategoria;
    b.style.background = sel ? CAT_COLOR[b.dataset.cat] : '#fff';
    b.style.color      = sel ? '#fff' : CAT_COLOR[b.dataset.cat];
    b.style.fontWeight = sel ? '700' : '500';
  });
  renderConfirmados();
}

// ── Modal de edición ──────────────────────────────────────────────────────────
let _modalData = null;
let _categoriaSeleccionada = 'Otros';
let _modalRotacion = 0;

function rotarModalImg() {
  _modalRotacion = (_modalRotacion + 90) % 360;
  const img = document.getElementById('modal-img');
  img.style.transform = _modalRotacion ? 'rotate(' + _modalRotacion + 'deg)' : '';
  // Ajustar max-height según orientación para que no se salga del contenedor
  img.style.maxWidth  = (_modalRotacion === 90 || _modalRotacion === 270) ? '60vh' : '100%';
  img.style.maxHeight = (_modalRotacion === 90 || _modalRotacion === 270) ? '100%'  : '60vh';
}

function abrirModalEdicion(p) {
  _modalData = p;
  _categoriaSeleccionada = p.categoria || 'Otros';
  const imgUrl = '/img?path=' + encodeURIComponent(p.path);
  const prov   = (p.proveedor && p.proveedor !== '[ilegible]' && p.proveedor !== '[sin_archivo]') ? p.proveedor : '';
  const cat    = p.categoria || 'Otros';
  _modalRotacion = 0;
  const mImg = document.getElementById('modal-img');
  mImg.src = imgUrl;
  mImg.style.transform = '';
  mImg.style.maxWidth  = '100%';
  mImg.style.maxHeight = '60vh';
  document.getElementById('modal-proveedor').value      = prov;
  document.getElementById('modal-monto').value          = p.monto != null ? p.monto : '';
  document.getElementById('modal-moneda').value         = p.moneda || 'CLP';
  // Formatear fecha como YYYY-MM-DD para date input
  let fechaVal = '';
  if (p.fecha) {
    // Si ya viene en YYYY-MM-DD, usar directo; si no, intentar parsear
    if (/^\\d{4}-\\d{2}-\\d{2}$/.test(p.fecha)) {
      fechaVal = p.fecha;
    } else {
      const d = new Date(p.fecha);
      if (!isNaN(d)) fechaVal = d.toISOString().slice(0, 10);
    }
  }
  const modalFecha = document.getElementById('modal-fecha');
  modalFecha.value = fechaVal;
  modalFecha.max   = new Date().toISOString().slice(0, 10);
  document.querySelectorAll('.modal-cat-btn').forEach(function(b) {
    const sel = b.dataset.cat === cat;
    b.style.background = sel ? CAT_COLOR[b.dataset.cat] : '#f3f4f6';
    b.style.color      = sel ? '#fff' : '#374151';
    b.style.fontWeight = sel ? '700' : '400';
  });
  document.getElementById('modal-edicion').style.display = 'flex';
  document.getElementById('modal-status').textContent    = '';
  // Keyboard shortcut: Enter para guardar
  function _modalEnterHandler(e) {
    if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
      guardarModalEdicion();
    }
  }
  const modal = document.getElementById('modal-edicion');
  modal._enterHandler && modal.removeEventListener('keydown', modal._enterHandler);
  modal._enterHandler = _modalEnterHandler;
  modal.addEventListener('keydown', _modalEnterHandler);
}

function cerrarModal() {
  document.getElementById('modal-edicion').style.display = 'none';
  _modalData = null;
}

function seleccionarCategoria(btn) {
  _categoriaSeleccionada = btn.dataset.cat;
  document.querySelectorAll('.modal-cat-btn').forEach(function(b) {
    const sel = b.dataset.cat === _categoriaSeleccionada;
    b.style.background = sel ? CAT_COLOR[b.dataset.cat] : '#f3f4f6';
    b.style.color      = sel ? '#fff' : '#374151';
    b.style.fontWeight = sel ? '700' : '400';
  });
}

async function borrarConfirmado() {
  if (!_modalData) return;
  const nombre = _modalData.proveedor || _modalData.name;
  if (!confirm('¿Borrar este recibo de "' + nombre + '"? Se eliminará de Dropbox y no se puede deshacer.')) return;
  const btn = document.getElementById('modal-borrar');
  btn.textContent = 'Borrando...';
  btn.disabled = true;
  const r = await fetch('/api/borrar-confirmado', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: _modalData.name})
  });
  if (r.ok) {
    _todosConfirmados = _todosConfirmados.filter(function(x){ return x.name !== _modalData.name; });
    renderConfirmados();
    renderFiltrosCat();
    cerrarModal();
  } else {
    btn.textContent = '🗑 Borrar recibo';
    btn.disabled = false;
    document.getElementById('modal-status').textContent = '✗ Error al borrar';
    document.getElementById('modal-status').style.color = '#ef4444';
  }
}

async function guardarModalEdicion() {
  if (!_modalData) return;
  const btn       = document.getElementById('modal-guardar');
  const proveedor = document.getElementById('modal-proveedor').value.trim();
  const monto     = parseFloat(document.getElementById('modal-monto').value) || null;
  const moneda    = document.getElementById('modal-moneda').value;
  const fecha     = document.getElementById('modal-fecha').value.trim();
  const categoria = _categoriaSeleccionada || 'Otros';
  btn.textContent = 'Guardando...';
  btn.disabled    = true;
  let r;
  try {
    r = await fetch('/api/editar-recibo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: _modalData.name, proveedor, monto, moneda, fecha, categoria})
    });
  } catch(err) {
    btn.textContent       = '✗ Error';
    btn.style.background  = '#ef4444';
    btn.disabled          = false;
    setTimeout(() => { btn.textContent = 'Guardar'; btn.style.background = '#4f46e5'; }, 2000);
    return;
  }
  if (r.ok) {
    btn.textContent      = '✓ Guardado';
    btn.style.background = '#10b981';
    btn.disabled         = false;
    const idx = _todosConfirmados.findIndex(function(x){ return x.name === _modalData.name; });
    if (idx >= 0) _todosConfirmados[idx] = Object.assign({}, _todosConfirmados[idx], {proveedor, monto, moneda, fecha, categoria, editado: true});
    renderConfirmados();
    renderFiltrosCat();
    setTimeout(cerrarModal, 1000);
  } else {
    btn.textContent       = '✗ Error';
    btn.style.background  = '#ef4444';
    btn.disabled          = false;
    setTimeout(() => { btn.textContent = 'Guardar'; btn.style.background = '#4f46e5'; }, 2000);
  }
}

async function confirmar(btnOrPath) {
  const path = typeof btnOrPath === 'string' ? btnOrPath : btnOrPath.closest('.card').dataset.path;
  const card = document.getElementById('card-' + pathId(path));
  if (!card) return;
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  const enRecibos   = !!card.closest('#grid-recibos');
  const enNoRecibos = !!card.closest('#grid-no-recibos');
  selRec.delete(path); selNoRec.delete(path); selCola.delete(path);
  card.remove();
  if (enRecibos)        nRec     = Math.max(0, nRec-1);
  else if (enNoRecibos) nNoRec   = Math.max(0, nNoRec-1);
  else                  nSinProc = Math.max(0, nSinProc-1);
  actualizarVista(); actualizarBotonesSel();
  await fetch('/api/confirm', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})
  });
  // Actualizar badge confirmados
  const badge = document.getElementById('badge-confirmados');
  badge.textContent = parseInt(badge.textContent || '0') + 1;
}

async function borrar(btnOrPath) {
  const path = typeof btnOrPath === 'string' ? btnOrPath : btnOrPath.closest('.card').dataset.path;
  const card = document.getElementById('card-' + pathId(path));
  if (!card) return;
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  const enRecibos   = !!card.closest('#grid-recibos');
  const enNoRecibos = !!card.closest('#grid-no-recibos');
  selRec.delete(path); selNoRec.delete(path); selCola.delete(path);
  card.remove();
  if (enRecibos)        nRec     = Math.max(0, nRec-1);
  else if (enNoRecibos) nNoRec   = Math.max(0, nNoRec-1);
  else                  nSinProc = Math.max(0, nSinProc-1);
  actualizarVista(); actualizarBotonesSel();
  await fetch('/api/delete', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})
  });
}

async function confirmarTodos() {
  const cards = [...document.getElementById('grid-recibos').querySelectorAll('.card')];
  const paths = cards.map(c => c.dataset.path);
  cards.forEach(c => { selRec.delete(c.dataset.path); c.remove(); });
  nRec = 0; actualizarVista(); actualizarBotonesSel();
  const badge = document.getElementById('badge-confirmados');
  badge.textContent = parseInt(badge.textContent||'0') + paths.length;
  await fetch('/api/confirm-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}
async function borrarTodos() {
  const cards = [...document.getElementById('grid-no-recibos').querySelectorAll('.card')];
  const paths = cards.map(c => c.dataset.path);
  cards.forEach(c => { selNoRec.delete(c.dataset.path); c.remove(); });
  nNoRec = 0; actualizarVista(); actualizarBotonesSel();
  await fetch('/api/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});
}

function abrirLightbox(url) {
  document.getElementById('lightbox-img').src = url;
  document.getElementById('lightbox').classList.add('open');
}
function cerrarLightbox() {
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lightbox-img').src = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') { cerrarLightbox(); cerrarModal(); } });

function recargar() { cargar(); }

// Cargar badge de confirmados al arrancar
fetch('/api/stats').then(r => r.json()).then(d => {
  document.getElementById('badge-confirmados').textContent = d.confirmados || 0;
});

cargar();
</script>

<!-- Modal edición recibo -->
<div id="modal-edicion" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;align-items:center;justify-content:center;padding:16px" onclick="if(event.target===this)cerrarModal()">
  <div style="background:#fff;border-radius:16px;max-width:860px;width:100%;max-height:90vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.3)">
    <!-- Header -->
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid #f0f0f0">
      <span style="font-size:15px;font-weight:700;color:#1a1a2e">Editar recibo</span>
      <button onclick="cerrarModal()" style="border:none;background:none;font-size:20px;cursor:pointer;color:#888;line-height:1">×</button>
    </div>
    <!-- Cuerpo -->
    <div style="display:flex;flex:1;overflow:hidden">
      <!-- Imagen -->
      <div style="width:45%;background:#f8f8f8;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:12px;gap:10px">
        <div style="overflow:hidden;display:flex;align-items:center;justify-content:center;flex:1;width:100%">
          <img id="modal-img" src="" style="max-width:100%;max-height:60vh;object-fit:contain;border-radius:8px;transition:transform .25s">
        </div>
        <button onclick="rotarModalImg()" title="Rotar imagen"
          style="display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:8px;border:1.5px solid #d1d5db;background:#fff;color:#555;font-size:12px;cursor:pointer;transition:.15s"
          onmouseenter="this.style.borderColor='#6366f1';this.style.color='#6366f1'"
          onmouseleave="this.style.borderColor='#d1d5db';this.style.color='#555'">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21.5 2v6h-6"/><path d="M21.34 15.57a10 10 0 1 1-.57-8.38"/></svg>
          Rotar
        </button>
      </div>
      <!-- Formulario -->
      <div style="flex:1;padding:20px;display:flex;flex-direction:column;gap:14px;overflow-y:auto">
        <div>
          <label style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px">Comercio</label>
          <input id="modal-proveedor" type="text" placeholder="Nombre del comercio"
                 style="width:100%;margin-top:5px;padding:8px 10px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:13px;box-sizing:border-box;outline:none"
                 onfocus="this.style.borderColor='#4f46e5'" onblur="this.style.borderColor='#e5e7eb'">
        </div>
        <div style="display:flex;gap:10px">
          <div style="flex:2">
            <label style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px">Monto</label>
            <input id="modal-monto" type="number" placeholder="0"
                   style="width:100%;margin-top:5px;padding:8px 10px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:13px;box-sizing:border-box;outline:none"
                   onfocus="this.style.borderColor='#4f46e5'" onblur="this.style.borderColor='#e5e7eb'">
          </div>
          <div style="flex:1">
            <label style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px">Moneda</label>
            <select id="modal-moneda" style="width:100%;margin-top:5px;padding:8px 10px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:13px;box-sizing:border-box;outline:none">
              <option value="CLP">🇨🇱 CLP</option>
              <option value="USD">🇺🇸 USD</option>
              <option value="PEN">🇵🇪 PEN</option>
              <option value="BRL">🇧🇷 BRL</option>
            </select>
          </div>
        </div>
        <div>
          <label style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px">Fecha de compra</label>
          <input id="modal-fecha" type="date" min="2020-01-01"
                 style="width:100%;margin-top:5px;padding:8px 10px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:13px;box-sizing:border-box;outline:none"
                 onfocus="this.style.borderColor='#4f46e5'" onblur="this.style.borderColor='#e5e7eb'">
        </div>
        <div>
          <label style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px">Categoría</label>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
            <button class="modal-cat-btn" data-cat="Viajes" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #3b82f6;cursor:pointer;transition:.15s">Viajes</button>
            <button class="modal-cat-btn" data-cat="Representación" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #f59e0b;cursor:pointer;transition:.15s">Representación</button>
            <button class="modal-cat-btn" data-cat="Gastos Varios de Oficina" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #10b981;cursor:pointer;transition:.15s">Gastos Varios de Oficina</button>
            <button class="modal-cat-btn" data-cat="Servicios Profesionales" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #8b5cf6;cursor:pointer;transition:.15s">Servicios Profesionales</button>
            <button class="modal-cat-btn" data-cat="Cuentas" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #ef4444;cursor:pointer;transition:.15s">Cuentas</button>
            <button class="modal-cat-btn" data-cat="Otros" onclick="seleccionarCategoria(this)" style="font-size:11px;padding:5px 12px;border-radius:20px;border:1.5px solid #6b7280;cursor:pointer;transition:.15s">Otros</button>
          </div>
        </div>
      </div>
    </div>
    <!-- Footer -->
    <div style="padding:14px 20px;border-top:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between">
      <div style="display:flex;align-items:center;gap:12px">
        <button id="modal-borrar" onclick="borrarConfirmado()"
                style="padding:8px 14px;border:1.5px solid #fca5a5;border-radius:8px;background:#fff;color:#ef4444;font-size:13px;cursor:pointer;transition:.15s"
                onmouseenter="this.style.background='#fef2f2'" onmouseleave="this.style.background='#fff'">
          🗑 Borrar recibo
        </button>
        <span id="modal-status" style="font-size:12px"></span>
      </div>
      <div style="display:flex;gap:10px">
        <button onclick="cerrarModal()" style="padding:8px 18px;border:1.5px solid #e5e7eb;border-radius:8px;background:#fff;font-size:13px;cursor:pointer">Cancelar</button>
        <button id="modal-guardar" onclick="guardarModalEdicion()" style="padding:8px 18px;border:none;border-radius:8px;background:#4f46e5;color:#fff;font-size:13px;font-weight:600;cursor:pointer">Guardar</button>
      </div>
    </div>
  </div>
</div>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


POLL_INTERVAL = 10 * 60  # cada 10 minutos

def background_worker():
    """Revisa Dropbox y clasifica fotos nuevas cada POLL_INTERVAL segundos."""
    import time as _time
    from extractor import extract_from_email
    _time.sleep(5)  # esperar que Flask arranque
    while True:
        try:
            logger.info("[worker] Revisando Dropbox...")
            dbx   = get_dbx()
            fotos = _list_folder(dbx, FOLDER)
            logger.info(f"[worker] {len(fotos)} fotos encontradas en Dropbox")

            nuevas = borradas_auto = clasificadas = errores = 0

            for p in fotos:
                nombre = p["name"]
                path   = p["path"]

                # ── Auto-borrar ≤2025 ──────────────────────────────────────
                anio_match = re.match(r'^(\d{4})', nombre)
                if anio_match and int(anio_match.group(1)) <= 2025:
                    try:
                        dbx.files_delete_v2(path)
                        db_upsert(path, nombre, p["fecha"], "borrado", cuenta="auto")
                        logger.info(f"[auto-borrado] {nombre}")
                        borradas_auto += 1
                        _time.sleep(0.5)  # evitar rate limit de Dropbox
                    except Exception:
                        pass  # ya no estaba en Dropbox, ignorar
                    continue

                # ── Saltar si ya clasificada ───────────────────────────────
                cached = db_get(path)
                if cached and cached["estado"] in ("recibo", "no_recibo", "borrado", "confirmado"):
                    continue

                # ── Clasificar con Mistral ─────────────────────────────────
                nuevas += 1
                logger.info(f"[clasificando] {nombre} → descargando...")
                try:
                    _, response = dbx.files_download(path)
                    data      = response.content
                    ext       = Path(path).suffix.lower().lstrip(".")
                    mime_type = EXT_TO_MIME.get(ext, "image/jpeg")

                    logger.info(f"[clasificando] {nombre} → enviando a Mistral...")
                    result    = extract_from_email({
                        "id": path, "asunto": nombre, "de": "dropbox",
                        "fecha": "", "cuerpo_texto": "",
                        "adjuntos": [{"data": data, "mime_type": mime_type}],
                    })

                    es_recibo = result.get("es_gasto", False)
                    estado    = "recibo" if es_recibo else "no_recibo"
                    proveedor = limpiar_proveedor(aplicar_aprendizaje(result.get("proveedor") or ""))
                    monto     = result.get("monto")
                    moneda    = result.get("moneda") or ""
                    conf      = result.get("confianza", 0)
                    icono     = "✓ RECIBO" if es_recibo else "✕ no recibo"
                    detalle   = f"{proveedor} {monto} {moneda}".strip() if proveedor else ""
                    logger.info(f"[Mistral] {nombre} → {icono}{(' — ' + detalle) if detalle else ''} (confianza {conf:.0%})")

                    db_upsert(path, nombre, p["fecha"], estado,
                              proveedor=proveedor,
                              monto=monto, moneda=moneda, confianza=conf)
                    clasificadas += 1
                    _time.sleep(1)  # respetar rate limit

                except Exception as e:
                    logger.error(f"[clasificando] Error en {nombre}: {e}")
                    errores += 1

            logger.info(
                f"[worker] Ciclo completo — nuevas: {nuevas}, "
                f"clasificadas: {clasificadas}, auto-borradas: {borradas_auto}, "
                f"errores: {errores}. Próxima revisión en {POLL_INTERVAL//60} min."
            )

            # ── Extracción retroactiva: recibos confirmados sin datos ──────────
            dbx2 = get_dbx()

            # 1. Archivos en /Recibos que no tienen registro en BD → insertar como confirmado
            fotos_recibos = _list_folder(dbx2, FOLDER_RECIBOS)
            nombres_bd = {r["name"] for r in sdb._get("fotos", {"estado": "eq.confirmado", "select": "name"})}
            for p in [f for f in fotos_recibos if f["name"] not in nombres_bd]:
                sdb.db_upsert(p["path"], p["name"], p["fecha"], "confirmado")
                logger.info(f"[retroactivo] registrando en BD: {p['name']}")

            # 2. Todos los confirmados sin proveedor/monto
            sin_datos_rows = sdb._get("fotos", {
                "estado":    "eq.confirmado",
                "select":    "name,path",
                "or":        "(proveedor.is.null,proveedor.eq.,monto.is.null)",
            })
            sin_datos = [(r["name"], r["path"]) for r in sin_datos_rows]

            if sin_datos:
                logger.info(f"[retroactivo] {len(sin_datos)} recibos confirmados sin datos — extrayendo...")
                for (nombre, path_original) in sin_datos:
                    # Intentar en /Recibos primero, luego en path original
                    path_recibo = f"{FOLDER_RECIBOS}/{nombre}"
                    try:
                        logger.info(f"[retroactivo] descargando {nombre}...")
                        try:
                            _, resp = dbx2.files_download(path_recibo)
                        except Exception:
                            _, resp = dbx2.files_download(path_original)
                        data    = comprimir_imagen(resp.content, max_px=1600, quality=75)
                        logger.info(f"[retroactivo] enviando a Mistral ({len(data)//1024}KB)...")
                        result  = extract_from_email({
                            "id": path_recibo, "asunto": nombre, "de": "dropbox",
                            "fecha": "", "cuerpo_texto": "",
                            "adjuntos": [{"data": data, "mime_type": "image/jpeg"}],
                        })
                        proveedor = limpiar_proveedor(aplicar_aprendizaje(result.get("proveedor") or ""))
                        monto     = result.get("monto")
                        moneda    = result.get("moneda") or ""
                        fecha     = result.get("fecha") or ""
                        if not proveedor and not monto:
                            proveedor = "[ilegible]"
                        sdb._patch("fotos", {"name": f"eq.{nombre}", "estado": "eq.confirmado"},
                                   {"proveedor": proveedor, "monto": monto, "moneda": moneda, "fecha": fecha})
                        logger.info(f"[retroactivo] ✓ {nombre} → {proveedor or '?'} {monto or '?'} {moneda} {fecha}")
                        _time.sleep(1)
                    except Exception as e:
                        if "not_found" in str(e):
                            # Intentar con thumbnail local cacheado
                            import hashlib
                            cache_key  = hashlib.md5(path_original.encode()).hexdigest() + ".jpg"
                            cache_file = THUMB_DIR / cache_key
                            if cache_file.exists():
                                try:
                                    logger.info(f"[retroactivo] {nombre}: usando thumbnail local...")
                                    data = cache_file.read_bytes()
                                    # Subir thumbnail a /Recibos para restaurar el archivo
                                    dest_recibo = f"{FOLDER_RECIBOS}/{nombre}"
                                    dbx2.files_upload(data, dest_recibo, mode=dbx_module.files.WriteMode.overwrite)
                                    logger.info(f"[retroactivo] ✓ {nombre}: restaurado en /Recibos ({len(data)//1024}KB)")
                                    logger.info(f"[retroactivo] enviando a Mistral ({len(data)//1024}KB)...")
                                    result  = extract_from_email({
                                        "id": path_original, "asunto": nombre, "de": "dropbox",
                                        "fecha": "", "cuerpo_texto": "",
                                        "adjuntos": [{"data": data, "mime_type": "image/jpeg"}],
                                    })
                                    proveedor = result.get("proveedor") or ""
                                    monto     = result.get("monto")
                                    moneda    = result.get("moneda") or ""
                                    fecha     = result.get("fecha") or ""
                                    sdb._patch("fotos", {"name": f"eq.{nombre}", "estado": "eq.confirmado"},
                                               {"proveedor": proveedor, "monto": monto, "moneda": moneda, "fecha": fecha})
                                    logger.info(f"[retroactivo] ✓ {nombre} (desde cache) → {proveedor or '?'} {monto or '?'} {moneda} {fecha}")
                                    _time.sleep(1)
                                except Exception as e2:
                                    logger.error(f"[retroactivo] ✗ {nombre}: {e2}")
                            else:
                                logger.warning(f"[retroactivo] ⚠ {nombre}: no está en Dropbox ni en cache local — archivo perdido")
                                sdb._patch("fotos", {"name": f"eq.{nombre}", "estado": "eq.confirmado"},
                                           {"proveedor": "[sin_archivo]"})
                        else:
                            logger.error(f"[retroactivo] ✗ {nombre}: {e}")

        except Exception as e:
            logger.error(f"[worker] Error general: {e}")

        # ── Migrar monedas antiguas a las 4 soportadas ────────────────────
        try:
            MONEDAS_VALIDAS = {"CLP", "USD", "PEN", "BRL"}
            MAPA_MONEDA = {"EUR": "USD", "ARS": "USD", "MXN": "USD",
                           "COP": "USD", "UYU": "USD", "GBP": "USD"}
            filas = sdb._get("fotos", {"estado": "eq.confirmado",
                                       "moneda": "not.is.null", "select": "name,moneda"})
            mig = 0
            for r in filas:
                nombre, moneda = r["name"], r["moneda"]
                if moneda not in MONEDAS_VALIDAS:
                    nueva = MAPA_MONEDA.get(moneda, "USD")
                    sdb._patch("fotos", {"name": f"eq.{nombre}"}, {"moneda": nueva})
                    mig += 1
                    logger.info(f"[moneda-retro] {nombre}: {moneda} → {nueva}")
            if mig:
                logger.info(f"[moneda-retro] {mig} monedas migradas")
        except Exception as e:
            logger.error(f"[moneda-retro] Error: {e}")

        # ── Aplicar aprendizaje retroactivo ────────────────────────────────
        try:
            reglas = sdb.get_reglas()
            if reglas:
                confirmados = sdb._get("fotos", {
                    "estado":    "eq.confirmado",
                    "select":    "name,proveedor,categoria",
                    "proveedor": "not.is.null",
                })
                actualizados = 0
                for r in confirmados:
                    nombre, prov_actual, cat_actual = r["name"], r["proveedor"], r.get("categoria")
                    if not prov_actual or prov_actual in ("", "[ilegible]", "[sin_archivo]"):
                        continue
                    prov_lower = prov_actual.lower()
                    nuevo_prov, nueva_cat = prov_actual, cat_actual
                    for patron, correcto, cat_ap in reglas:
                        if patron.lower() in prov_lower or prov_lower in patron.lower():
                            if correcto and correcto != prov_actual: nuevo_prov = correcto
                            if cat_ap and cat_ap != cat_actual:      nueva_cat  = cat_ap
                            break
                    if nuevo_prov != prov_actual or nueva_cat != cat_actual:
                        sdb._patch("fotos", {"name": f"eq.{nombre}", "estado": "eq.confirmado"},
                                   {"proveedor": nuevo_prov, "categoria": nueva_cat})
                        actualizados += 1
                        logger.info(f"[aprendizaje-retro] {nombre}: '{prov_actual}'→'{nuevo_prov}' | '{cat_actual}'→'{nueva_cat}'")
                if actualizados:
                    logger.info(f"[aprendizaje-retro] {actualizados} recibos actualizados")
        except Exception as e:
            logger.error(f"[aprendizaje-retro] Error: {e}")

        _time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    init_db()
    # Worker background: revisa Dropbox y clasifica cada 10 min
    threading.Thread(target=background_worker, daemon=True).start()
    print("\n  Abre http://localhost:5001 en tu browser\n")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
