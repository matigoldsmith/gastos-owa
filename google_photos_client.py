"""
Cliente Google Photos API.

Flujo optimizado (sin costo de filtro):
  1. OAuth2 con credentials.json de Google Cloud Console
  2. Consulta la API con filtro nativo RECEIPTS + DOCUMENTS
     → Google ya clasificó las fotos gratis con su propio modelo
  3. Solo descarga las fotos que Google identificó como recibos/documentos
  4. Las pasa a Gemini para extracción completa (proveedor, monto, fecha)

Setup requerido (Google Cloud Console):
  - Habilitar "Google Photos Library API"
  - Agregar scope: https://www.googleapis.com/auth/photoslibrary.readonly
    al mismo OAuth2 de credentials.json
"""
import base64
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import google.generativeai as genai
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import GMAIL_CREDENTIALS_FILE

logger = logging.getLogger(__name__)

PHOTOS_SCOPES     = ["https://www.googleapis.com/auth/photoslibrary.readonly"]
PHOTOS_TOKEN_FILE = Path(__file__).parent / "token_photos.json"
PHOTOS_API_BASE   = "https://photoslibrary.googleapis.com/v1"

# Categorías nativas de Google Photos que indican documentos/recibos
# Google las clasifica automáticamente sin costo adicional
RECEIPT_CATEGORIES = ["RECEIPTS", "DOCUMENTS", "SCREENSHOTS"]


class GooglePhotosClient:
    def __init__(self):
        self.creds   = self._authenticate()
        self.session = requests.Session()

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _authenticate(self) -> Credentials:
        creds = None

        if PHOTOS_TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(PHOTOS_TOKEN_FILE), PHOTOS_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Token Google Photos refrescado.")
            else:
                if not GMAIL_CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"No encontré {GMAIL_CREDENTIALS_FILE}. "
                        "Descárgalo desde Google Cloud Console → APIs → Credenciales."
                    )
                flow  = InstalledAppFlow.from_client_secrets_file(
                    str(GMAIL_CREDENTIALS_FILE), PHOTOS_SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Autenticación Google Photos completada.")

            PHOTOS_TOKEN_FILE.write_text(creds.to_json())

        return creds

    def _headers(self) -> dict:
        if self.creds.expired:
            self.creds.refresh(Request())
            PHOTOS_TOKEN_FILE.write_text(self.creds.to_json())
        return {"Authorization": f"Bearer {self.creds.token}"}

    # ── Listado con filtro nativo de Google ────────────────────────────────────

    def list_receipt_photos(
        self,
        since: datetime,
        page_size: int = 100,
    ) -> list[dict]:
        """
        Usa el filtro nativo de Google Photos para traer solo fotos
        clasificadas como RECEIPTS, DOCUMENTS o SCREENSHOTS.
        Google hace esta clasificación gratis con su propio modelo.
        """
        start = since.astimezone(timezone.utc)
        end   = datetime.now(timezone.utc)

        body = {
            "pageSize": page_size,
            "filters": {
                "dateFilter": {
                    "ranges": [{
                        "startDate": {"year": start.year, "month": start.month, "day": start.day},
                        "endDate":   {"year": end.year,   "month": end.month,   "day": end.day},
                    }]
                },
                "contentFilter": {
                    # Filtro nativo — Google ya las clasificó
                    "includedContentCategories": RECEIPT_CATEGORIES,
                },
                "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
            },
        }

        items      = []
        page_token = None

        while True:
            if page_token:
                body["pageToken"] = page_token

            resp = self.session.post(
                f"{PHOTOS_API_BASE}/mediaItems:search",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            items.extend(data.get("mediaItems", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        logger.info(
            f"Google Photos: {len(items)} fotos clasificadas como "
            f"{RECEIPT_CATEGORIES} desde {since.date()}"
        )
        return items

    def list_albums(self) -> list[dict]:
        """Lista álbumes — útil si quieres usar un álbum 'Recibos' en vez del filtro automático."""
        resp = self.session.get(f"{PHOTOS_API_BASE}/albums", headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("albums", [])

    def list_photos_from_album(self, album_id: str, page_size: int = 100) -> list[dict]:
        """Alternativa: fotos de un álbum específico (si prefieres curación manual)."""
        items      = []
        page_token = None
        while True:
            body = {"albumId": album_id, "pageSize": page_size}
            if page_token:
                body["pageToken"] = page_token
            resp = self.session.post(
                f"{PHOTOS_API_BASE}/mediaItems:search",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("mediaItems", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    # ── Descarga ───────────────────────────────────────────────────────────────

    def download_photo(self, media_item: dict, max_size: int = 800) -> bytes:
        """
        Descarga la foto redimensionada. 800px es suficiente para extracción
        de texto en recibos y reduce el tamaño significativamente.
        """
        url  = f"{media_item['baseUrl']}=w{max_size}-h{max_size}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    # ── Pipeline principal ─────────────────────────────────────────────────────

    def get_receipt_photos(
        self,
        processed_ids: set[str],
        since: Optional[datetime] = None,
        album_id: Optional[str]   = None,
    ) -> list[dict]:
        """
        Retorna lista de dicts listos para extractor.extract_from_email().

        Si album_id: usa fotos del álbum (curación manual).
        Si no: usa filtro automático de Google (RECEIPTS + DOCUMENTS).
        """
        since = since or (datetime.now(timezone.utc) - timedelta(days=30))

        if album_id:
            all_items = self.list_photos_from_album(album_id)
        else:
            all_items = self.list_receipt_photos(since)

        new_items = [it for it in all_items if it["id"] not in processed_ids]
        logger.info(f"Fotos nuevas a procesar: {len(new_items)}")

        results = []
        for item in new_items:
            try:
                image_bytes = self.download_photo(item)
                results.append(_build_photo_data(item, image_bytes))
            except Exception as e:
                logger.error(f"Error descargando foto {item['id']}: {e}")

        return results


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_photo_data(media_item: dict, image_bytes: bytes) -> dict:
    """Construye dict compatible con extractor.extract_from_email()."""
    meta      = media_item.get("mediaMetadata", {})
    timestamp = meta.get("creationTime", "")
    fecha_str = timestamp[:10] if timestamp else ""

    return {
        "id":           media_item["id"],
        "asunto":       media_item.get("filename", "foto"),
        "de":           "google_photos",
        "fecha":        timestamp,
        "cuerpo_texto": f"Foto tomada el {fecha_str}. Archivo: {media_item.get('filename', '')}",
        "adjuntos": [{
            "filename":  media_item.get("filename", "foto.jpg"),
            "mime_type": "image/jpeg",
            "data":      image_bytes,
        }],
        "_fuente": "google_photos",
    }
