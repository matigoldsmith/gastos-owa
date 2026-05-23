#!/usr/bin/env python3
"""
Setup OAuth2 de Dropbox — ejecutar UNA SOLA VEZ.

Obtiene el refresh token y lo imprime para poner en .env

Instrucciones:
  1. Crea una app en https://www.dropbox.com/developers/apps
     - Choose an API: "Scoped access"
     - Choose the type: "Full Dropbox"
     - Name: "GastosOwa" (o cualquier nombre)
  2. En la app, ve a Settings y copia App Key y App Secret
  3. En Permissions, activa: files.content.read
  4. Pon App Key y App Secret en .env como DROPBOX_APP_KEY y DROPBOX_APP_SECRET
  5. Ejecuta: python dropbox_auth.py
  6. Visita la URL, autoriza, pega el código de autorización
  7. Copia el DROPBOX_REFRESH_TOKEN al .env
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    import dropbox
    from dropbox import oauth
except ImportError:
    print("Error: Instala el SDK de Dropbox primero:")
    print("  source venv/bin/activate && pip install dropbox")
    sys.exit(1)

APP_KEY    = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")

if not APP_KEY or not APP_SECRET:
    print("ERROR: Falta DROPBOX_APP_KEY o DROPBOX_APP_SECRET en .env")
    print()
    print("Pasos:")
    print("  1. Ve a https://www.dropbox.com/developers/apps")
    print("  2. Crea una app (Scoped access → Full Dropbox)")
    print("  3. Copia App Key y App Secret")
    print("  4. Agrégalos al .env:")
    print("     DROPBOX_APP_KEY=tu_app_key")
    print("     DROPBOX_APP_SECRET=tu_app_secret")
    sys.exit(1)

print("=" * 60)
print("  Setup OAuth2 de Dropbox para Gastos OWA")
print("=" * 60)
print()

auth_flow = oauth.DropboxOAuth2FlowNoRedirect(
    consumer_key=APP_KEY,
    consumer_secret=APP_SECRET,
    token_access_type="offline",   # obtiene refresh token
)

authorize_url = auth_flow.start()
print("1. Abre esta URL en tu navegador:")
print()
print(f"   {authorize_url}")
print()
print("2. Autoriza la app y copia el código que aparece")
print()

auth_code = input("3. Pega el código aquí: ").strip()

try:
    result = auth_flow.finish(auth_code)
except Exception as e:
    print(f"\nError al canjear el código: {e}")
    sys.exit(1)

print()
print("=" * 60)
print("✅ ¡Éxito! Agrega esto a tu .env:")
print()
print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")
print()
print("=" * 60)

# Verificar que funciona
try:
    dbx     = dropbox.Dropbox(
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        oauth2_refresh_token=result.refresh_token,
    )
    account = dbx.users_get_current_account()
    print(f"Cuenta conectada: {account.name.display_name} ({account.email})")
except Exception as e:
    print(f"Advertencia — error al verificar: {e}")
