"""
Configuración centralizada. Todos los parámetros vienen de .env o tienen defaults.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# ── Base de datos ──────────────────────────────────────────────────────────────
DB_PATH = BASE_DIR / os.getenv("DB_FILENAME", "gastos.db")

# ── Gemini API ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ── Gmail OAuth2 ───────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE = BASE_DIR / os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE       = BASE_DIR / os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES           = ["https://www.googleapis.com/auth/gmail.readonly"]

# Query para filtrar emails relevantes (ajustar según necesidad)
GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    "has:attachment (invoice OR factura OR boleta OR receipt OR cobro) newer_than:60d"
)

# ── Procesamiento ──────────────────────────────────────────────────────────────
BATCH_SIZE              = int(os.getenv("BATCH_SIZE", "20"))
CHECK_INTERVAL_MINUTES  = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
MAX_BODY_CHARS          = int(os.getenv("MAX_BODY_CHARS", "3000"))
MAX_ATTACHMENTS         = int(os.getenv("MAX_ATTACHMENTS", "3"))

# ── Web app ────────────────────────────────────────────────────────────────────
WEB_HOST   = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT   = int(os.getenv("WEB_PORT", "5000"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ITEMS_PER_PAGE = int(os.getenv("ITEMS_PER_PAGE", "50"))

# ── Logs ───────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = BASE_DIR / "logs" / "gastos.log"
