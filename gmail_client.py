"""
Cliente Gmail con OAuth2.
Maneja autenticación, listado y descarga de emails con adjuntos.
"""
import base64
import logging
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, MAX_BODY_CHARS, MAX_ATTACHMENTS

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()

    # ── Autenticación ──────────────────────────────────────────────────────────

    def _authenticate(self):
        creds = None

        if GMAIL_TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Token Gmail refrescado.")
            else:
                if not GMAIL_CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"No se encontró {GMAIL_CREDENTIALS_FILE}. "
                        "Descárgalo desde Google Cloud Console → APIs → Credenciales."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("Autenticación Gmail completada.")

            GMAIL_TOKEN_FILE.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── Listado de emails ──────────────────────────────────────────────────────

    def get_unprocessed_ids(self, query: str, processed_ids: set[str]) -> list[str]:
        """Retorna IDs de emails que coinciden con query y no han sido procesados."""
        result_ids = []
        page_token = None

        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token

            response = self.service.users().messages().list(**kwargs).execute()
            messages = response.get("messages", [])

            for msg in messages:
                if msg["id"] not in processed_ids:
                    result_ids.append(msg["id"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"Emails nuevos encontrados: {len(result_ids)}")
        return result_ids

    # ── Extracción de datos ────────────────────────────────────────────────────

    def get_email_data(self, message_id: str) -> dict:
        """
        Retorna dict con:
          id, asunto, de, fecha, cuerpo_texto, adjuntos (lista de dicts)
        """
        msg = self.service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

        data = {
            "id":          message_id,
            "asunto":      headers.get("Subject", ""),
            "de":          headers.get("From", ""),
            "fecha":       headers.get("Date", ""),
            "cuerpo_texto": "",
            "adjuntos":    [],
        }

        self._parse_parts(msg["payload"], data, message_id)

        # Truncar cuerpo para no pasarle texto infinito a Claude
        data["cuerpo_texto"] = data["cuerpo_texto"][:MAX_BODY_CHARS]
        # Limitar adjuntos
        data["adjuntos"] = data["adjuntos"][:MAX_ATTACHMENTS]

        return data

    def _parse_parts(self, payload: dict, result: dict, message_id: str):
        """Recorre recursivamente el payload MIME extrayendo texto y adjuntos."""
        mime = payload.get("mimeType", "")

        if mime == "text/plain":
            raw = payload.get("body", {}).get("data", "")
            if raw:
                result["cuerpo_texto"] += base64.urlsafe_b64decode(raw).decode("utf-8", errors="ignore")

        elif mime in ("application/pdf", "image/jpeg", "image/png", "image/jpg", "image/webp"):
            body = payload.get("body", {})
            filename     = payload.get("filename") or f"adjunto_{len(result['adjuntos']) + 1}"
            attachment_id = body.get("attachmentId")

            if attachment_id:
                att  = self.service.users().messages().attachments().get(
                    userId="me", messageId=message_id, id=attachment_id
                ).execute()
                data = base64.urlsafe_b64decode(att["data"])
            else:
                raw  = body.get("data", "")
                data = base64.urlsafe_b64decode(raw) if raw else b""

            if data:
                result["adjuntos"].append({
                    "filename":  filename,
                    "mime_type": mime,
                    "data":      data,
                })

        # Recursión sobre partes del multipart
        for part in payload.get("parts", []):
            self._parse_parts(part, result, message_id)
