"""
Cliente Dropbox para recibir fotos automáticamente.

Flujo:
  iPhone → Dropbox Camera Upload → Dropbox Cloud
                                        ↓
                              dropbox_client.py (descarga nuevas)
                                        ↓
                              extractor.py (Gemini detecta recibos)

Autenticación: OAuth2 con refresh token (no expira).
Setup: ejecutar 'python dropbox_auth.py' una sola vez.
"""
import logging
import os
from pathlib import Path
from typing import Optional

import dropbox
from dropbox import files as dbx_files
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_APP_KEY      = os.getenv("DROPBOX_APP_KEY")
_APP_SECRET   = os.getenv("DROPBOX_APP_SECRET")
_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
_FOLDER       = os.getenv("DROPBOX_FOLDER", "/Camera Uploads")

# Formatos de imagen que puede analizar Gemini
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
_EXT_TO_MIME = {
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}


def _get_client() -> dropbox.Dropbox:
    if not all([_APP_KEY, _APP_SECRET, _REFRESH_TOKEN]):
        raise EnvironmentError(
            "Faltan DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN en .env\n"
            "Ejecuta: python dropbox_auth.py"
        )
    return dropbox.Dropbox(
        app_key=_APP_KEY,
        app_secret=_APP_SECRET,
        oauth2_refresh_token=_REFRESH_TOKEN,
    )


_DAYS_BACK = int(os.getenv("DROPBOX_DAYS_BACK", "60"))


def get_new_photos(processed_paths: set[str], max_files: int = 50, since: "datetime | None" = None) -> list[dict]:
    """
    Lista la carpeta DROPBOX_FOLDER y descarga las fotos no procesadas
    de los últimos DROPBOX_DAYS_BACK días (default 60).

    Retorna lista de dicts compatibles con extract_from_email():
    {
        "id":            path único en Dropbox,
        "asunto":        nombre de archivo,
        "de":            "dropbox",
        "fecha":         "YYYY-MM-DD" (fecha del archivo),
        "cuerpo_texto":  "",
        "adjuntos":      [{"data": bytes, "mime_type": str}],
    }
    """
    if not all([_APP_KEY, _APP_SECRET, _REFRESH_TOKEN]):
        logger.error("Credenciales Dropbox no configuradas. Ejecuta: python dropbox_auth.py")
        return []

    from datetime import datetime, timedelta, timezone
    cutoff = since if since is not None else datetime.now(timezone.utc) - timedelta(days=_DAYS_BACK)

    dbx     = _get_client()
    results = []

    try:
        res = dbx.files_list_folder(_FOLDER, limit=100)
    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error listando carpeta Dropbox '{_FOLDER}': {e}")
        return []

    while True:
        for entry in res.entries:
            if len(results) >= max_files:
                break

            # Solo archivos (no carpetas)
            if not isinstance(entry, dbx_files.FileMetadata):
                continue

            # Solo imágenes
            ext = Path(entry.name).suffix.lower().lstrip(".")
            if ext not in _IMAGE_EXTS:
                continue

            # Filtrar por fecha — ignorar fotos antiguas
            modified = entry.client_modified
            if modified:
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                if modified < cutoff:
                    continue

            path = entry.path_lower

            # Ya procesado
            if path in processed_paths:
                continue

            # Descargar
            try:
                _, response = dbx.files_download(path)
                data      = response.content
                mime_type = _EXT_TO_MIME.get(ext, "image/jpeg")
                fecha     = entry.client_modified.strftime("%Y-%m-%d") if entry.client_modified else None

                results.append({
                    "id":           path,
                    "asunto":       entry.name,
                    "de":           "dropbox",
                    "fecha":        fecha,
                    "cuerpo_texto": "",
                    "adjuntos":     [{"data": data, "mime_type": mime_type}],
                })
                logger.info(f"Dropbox ↓ {entry.name} ({len(data)//1024} KB)")

            except Exception as e:
                logger.error(f"Error descargando {path}: {e}")

        if not res.has_more or len(results) >= max_files:
            break

        try:
            res = dbx.files_list_folder_continue(res.cursor)
        except Exception as e:
            logger.error(f"Error paginando Dropbox: {e}")
            break

    logger.info(f"Dropbox: {len(results)} fotos nuevas encontradas (últimos {_DAYS_BACK} días)")
    return results


def delete_photo(path: str) -> bool:
    """Borra una foto de Dropbox (solo para fotos que NO son recibos)."""
    try:
        dbx = _get_client()
        dbx.files_delete_v2(path)
        logger.debug(f"Dropbox 🗑 eliminada: {path}")
        return True
    except Exception as e:
        logger.error(f"Error borrando {path}: {e}")
        return False


def move_photo(path: str, es_recibo: bool) -> bool:
    """
    Mueve la foto a la carpeta correcta según si es recibo o no:
      - /Recibos/     → para revisar recibos procesados
      - /No Recibos/  → para revisar falsos positivos/negativos
    """
    from pathlib import Path as _Path
    destino = "/Recibos" if es_recibo else "/No Recibos"
    try:
        dbx      = _get_client()
        filename = _Path(path).name
        dest     = f"{destino}/{filename}"
        dbx.files_move_v2(path, dest, autorename=True)
        logger.info(f"Dropbox → {dest}")
        return True
    except Exception as e:
        logger.error(f"Error moviendo {path} a {destino}: {e}")
        return False


def test_connection() -> bool:
    """Verifica que las credenciales funcionan. Retorna True si OK."""
    try:
        dbx     = _get_client()
        account = dbx.users_get_current_account()
        logger.info(f"Dropbox conectado: {account.name.display_name} ({account.email})")
        return True
    except Exception as e:
        logger.error(f"Error de conexión Dropbox: {e}")
        return False
