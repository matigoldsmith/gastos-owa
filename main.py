#!/usr/bin/env python3
"""
Orquestador principal del Expense Tracker.

Uso:
  python main.py                    # Gmail + Dropbox, una vez
  python main.py --loop             # loop cada CHECK_INTERVAL_MINUTES minutos
  python main.py --only-gmail       # solo Gmail
  python main.py --only-dropbox     # solo Dropbox Camera Upload
  python main.py --only-photos      # solo Google Photos (legacy, deprecado)
"""
import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    DB_PATH, GMAIL_QUERY, BATCH_SIZE,
    CHECK_INTERVAL_MINUTES, LOG_LEVEL, LOG_FILE
)
from gmail_client import GmailClient
from extractor import extract_batch, extract_from_email
from clasificador import clasificar

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    schema_path = Path(__file__).parent / "schema.sql"
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(schema_path.read_text())
    db.commit()
    logger.info(f"BD inicializada: {DB_PATH}")
    return db


def get_email_processed_ids(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT gmail_message_id FROM emails_procesados").fetchall()}


def get_photo_processed_ids(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT photos_media_id FROM fotos_procesadas").fetchall()}


def get_dropbox_processed_paths(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT dropbox_path FROM dropbox_procesadas").fetchall()}


def guardar_gasto(
    db: sqlite3.Connection,
    source_id: str,
    fuente: str,
    ext: dict,
    cat_id: int,
    confianza: float,
):
    db.execute(
        """INSERT INTO gastos
           (fecha, proveedor, monto, moneda, categoria_id, descripcion,
            fuente, email_id, confianza, revisado)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            ext.get("fecha"),
            ext.get("proveedor"),
            ext.get("monto"),
            ext.get("moneda", "CLP"),
            cat_id,
            ext.get("descripcion"),
            fuente,
            source_id,
            confianza,
        ),
    )


def marcar_email(db: sqlite3.Connection, email_data: dict, resultado: str):
    db.execute(
        "INSERT OR IGNORE INTO emails_procesados (gmail_message_id, fecha, asunto, resultado) VALUES (?,?,?,?)",
        (email_data["id"], email_data.get("fecha"), email_data.get("asunto", ""), resultado),
    )


def marcar_foto(db: sqlite3.Connection, photo_data: dict, resultado: str):
    db.execute(
        "INSERT OR IGNORE INTO fotos_procesadas (photos_media_id, filename, fecha_foto, resultado) VALUES (?,?,?,?)",
        (photo_data["id"], photo_data.get("asunto", ""), photo_data.get("fecha"), resultado),
    )


def marcar_dropbox(db: sqlite3.Connection, photo_data: dict, resultado: str):
    db.execute(
        "INSERT OR IGNORE INTO dropbox_procesadas (dropbox_path, filename, fecha_foto, resultado) VALUES (?,?,?,?)",
        (photo_data["id"], photo_data.get("asunto", ""), photo_data.get("fecha"), resultado),
    )


# ── Pipeline Gmail ─────────────────────────────────────────────────────────────

def run_gmail_pipeline(db: sqlite3.Connection, gmail: GmailClient):
    logger.info("── Gmail pipeline iniciado ──")
    processed_ids = get_email_processed_ids(db)
    new_ids       = gmail.get_unprocessed_ids(GMAIL_QUERY, processed_ids)

    if not new_ids:
        logger.info("Gmail: sin emails nuevos.")
        return

    for batch_num, start in enumerate(range(0, len(new_ids), BATCH_SIZE), 1):
        batch_ids   = new_ids[start : start + BATCH_SIZE]
        logger.info(f"Gmail batch {batch_num}: {len(batch_ids)} emails")

        emails_data = []
        for msg_id in batch_ids:
            try:
                emails_data.append(gmail.get_email_data(msg_id))
            except Exception as e:
                logger.error(f"Error descargando email {msg_id}: {e}")

        pares            = extract_batch(emails_data)
        gastos_guardados = 0

        for email_data, ext in pares:
            try:
                if ext.get("es_gasto"):
                    cat_id, conf, metodo = clasificar(
                        db,
                        ext.get("proveedor", ""),
                        ext.get("descripcion", ""),
                        ext.get("monto", 0) or 0,
                        ext.get("moneda", "CLP"),
                    )
                    guardar_gasto(db, email_data["id"], "gmail", ext, cat_id, conf)
                    marcar_email(db, email_data, "ok")
                    gastos_guardados += 1
                    logger.info(f"✓ Gmail: {ext.get('proveedor','?')} ${ext.get('monto','?')} [{metodo}]")
                else:
                    marcar_email(db, email_data, "sin_gasto")
            except Exception as e:
                logger.error(f"Error procesando email {email_data.get('id')}: {e}")
                marcar_email(db, email_data, "error")

        db.commit()
        logger.info(f"Gmail batch {batch_num}: {gastos_guardados} gastos guardados")


# ── Pipeline Dropbox ───────────────────────────────────────────────────────────

def run_dropbox_pipeline(db: sqlite3.Connection, history: bool = False, dry_run: bool = False):
    logger.info("── Dropbox pipeline iniciado ──")

    try:
        from dropbox_client import get_new_photos, test_connection
    except ImportError as e:
        logger.error(f"dropbox_client no disponible: {e}")
        return

    if not test_connection():
        logger.error("No se pudo conectar a Dropbox. Verifica credenciales en .env")
        return

    processed_paths = get_dropbox_processed_paths(db)

    if history:
        from datetime import datetime, timezone
        since = datetime(2025, 5, 15, tzinfo=timezone.utc)
        logger.info(f"Modo histórico: procesando fotos desde {since.date()}")
        fotos = get_new_photos(processed_paths, max_files=5000, since=since)
    else:
        fotos = get_new_photos(processed_paths)

    if not fotos:
        logger.info("Dropbox: sin fotos nuevas.")
        return

    if dry_run:
        logger.info("🔍 MODO DRY-RUN — no se guarda ni borra nada")
        print(f"\n{'─'*60}")
        print(f"  DRY-RUN: analizando {len(fotos)} fotos")
        print(f"{'─'*60}")

    gastos_guardados = 0
    for photo_data in fotos:
        try:
            ext = extract_from_email(photo_data)

            if dry_run:
                es_gasto = ext.get("es_gasto", False)
                icono    = "✅ RECIBO" if es_gasto else "❌ no recibo"
                print(f"{icono} | {photo_data['asunto']} | "
                      f"{ext.get('proveedor','?')} | "
                      f"${ext.get('monto','?')} {ext.get('moneda','')} | "
                      f"confianza={ext.get('confianza',0):.0%}")
                continue  # no guardar, no borrar

            if ext.get("es_gasto"):
                cat_id, conf, metodo = clasificar(
                    db,
                    ext.get("proveedor", ""),
                    ext.get("descripcion", ""),
                    ext.get("monto", 0) or 0,
                    ext.get("moneda", "CLP"),
                )
                guardar_gasto(db, photo_data["id"], "dropbox", ext, cat_id, conf)
                marcar_dropbox(db, photo_data, "ok")
                gastos_guardados += 1
                logger.info(
                    f"✓ Dropbox: {ext.get('proveedor','?')} "
                    f"${ext.get('monto','?')} [{metodo}] — {photo_data['asunto']}"
                )
            else:
                logger.debug(f"No es recibo: {photo_data['asunto']}")
                marcar_dropbox(db, photo_data, "sin_gasto")

            # Gestión de espacio en Dropbox:
            # - Recibos    → mover a /Recibos/ (preservar para auditoría)
            # - No recibos → borrar inmediatamente (liberar espacio para nuevas fotos)
            from dropbox_client import move_photo, delete_photo
            if ext.get("es_gasto"):
                move_photo(photo_data["id"], es_recibo=True)
            else:
                delete_photo(photo_data["id"])

        except Exception as e:
            logger.error(f"Error procesando {photo_data.get('asunto','?')}: {e}")
            if not dry_run:
                marcar_dropbox(db, photo_data, "error")

    if dry_run:
        print(f"{'─'*60}")
        print("  Dry-run completado. Revisa los resultados arriba.")
        print("  Si se ve bien: python main.py --only-dropbox --history")
        print(f"{'─'*60}\n")
        return

    db.commit()
    logger.info(f"Dropbox: {gastos_guardados} gastos guardados de {len(fotos)} fotos")


# ── Pipeline Google Photos (legacy) ───────────────────────────────────────────

def run_photos_pipeline(db: sqlite3.Connection, album_id: str = None):
    logger.info("── Google Photos pipeline (legacy) ──")
    try:
        from google_photos_client import GooglePhotosClient
        photos = GooglePhotosClient()
    except Exception as e:
        logger.error(f"No se pudo inicializar Google Photos: {e}")
        return

    processed_ids = get_photo_processed_ids(db)
    since         = datetime.now(timezone.utc) - timedelta(days=30)

    photo_items = photos.get_receipt_photos(
        processed_ids=processed_ids,
        since=since,
        album_id=album_id,
    )

    if not photo_items:
        logger.info("Google Photos: sin fotos nuevas.")
        return

    gastos_guardados = 0
    for photo_data in photo_items:
        try:
            if not photo_data.get("_es_recibo"):
                marcar_foto(db, photo_data, "sin_gasto")
                continue

            ext = extract_from_email(photo_data)

            if ext.get("es_gasto"):
                cat_id, conf, metodo = clasificar(
                    db,
                    ext.get("proveedor", ""),
                    ext.get("descripcion", ""),
                    ext.get("monto", 0) or 0,
                    ext.get("moneda", "CLP"),
                )
                guardar_gasto(db, photo_data["id"], "google_photos", ext, cat_id, conf)
                marcar_foto(db, photo_data, "ok")
                gastos_guardados += 1
                logger.info(f"✓ Foto: {ext.get('proveedor','?')} ${ext.get('monto','?')} [{metodo}]")
            else:
                marcar_foto(db, photo_data, "sin_gasto")

        except Exception as e:
            logger.error(f"Error procesando foto {photo_data.get('id')}: {e}")
            marcar_foto(db, photo_data, "error")

    db.commit()
    logger.info(f"Google Photos: {gastos_guardados} gastos guardados")


# ── Pipeline completo ──────────────────────────────────────────────────────────

def run_all(db: sqlite3.Connection, gmail: GmailClient = None,
            skip_gmail: bool = False, skip_dropbox: bool = False,
            skip_photos: bool = True, photos_album: str = None,
            history: bool = False, dry_run: bool = False):
    logger.info("=== Pipeline completo iniciado ===")
    if not skip_gmail and gmail:
        run_gmail_pipeline(db, gmail)
    if not skip_dropbox:
        run_dropbox_pipeline(db, history=history, dry_run=dry_run)
    if not skip_photos:
        run_photos_pipeline(db, album_id=photos_album)
    logger.info("=== Pipeline completo finalizado ===")


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Expense Tracker — Orquestador")
    parser.add_argument("--loop",          action="store_true", help="Correr en loop")
    parser.add_argument("--only-gmail",    action="store_true", help="Solo procesar Gmail")
    parser.add_argument("--only-dropbox",  action="store_true", help="Solo procesar Dropbox")
    parser.add_argument("--only-photos",   action="store_true", help="Solo Google Photos (legacy)")
    parser.add_argument("--photos-album",  default=None,        help="ID de álbum de Google Photos")
    parser.add_argument("--history",       action="store_true", help="Procesar todas las fotos históricas de Dropbox sin límite de fecha")
    parser.add_argument("--dry-run",       action="store_true", help="Analizar fotos sin guardar ni mover nada (solo mostrar resultados)")
    args = parser.parse_args()

    db    = init_db()
    gmail = None

    skip_gmail   = args.only_dropbox or args.only_photos
    skip_dropbox = args.only_gmail   or args.only_photos
    skip_photos  = not args.only_photos  # por defecto Google Photos está desactivado

    if not skip_gmail:
        gmail = GmailClient()

    if not args.loop:
        run_all(db, gmail,
                skip_gmail=skip_gmail,
                skip_dropbox=skip_dropbox,
                skip_photos=skip_photos,
                photos_album=args.photos_album,
                history=args.history,
                dry_run=args.dry_run)
    else:
        logger.info(f"Modo loop: cada {CHECK_INTERVAL_MINUTES} min")
        while True:
            try:
                run_all(db, gmail,
                        skip_gmail=skip_gmail,
                        skip_dropbox=skip_dropbox,
                        skip_photos=skip_photos,
                        photos_album=args.photos_album,
                        history=args.history,
                        dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"Error en pipeline (continuando): {e}")
            logger.info(f"Próxima ejecución en {CHECK_INTERVAL_MINUTES} min...")
            time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
