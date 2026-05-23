#!/usr/bin/env python3
"""
Test de conexión a Google Photos.
Corre este script ANTES de usar el sistema para verificar que todo funciona.

Requisitos previos:
  1. pip install -r requirements.txt
  2. credentials.json en esta carpeta (descargado de Google Cloud Console)
  3. Google Photos Library API habilitada en el mismo proyecto de Google Cloud
"""
from google_photos_client import GooglePhotosClient

print("Conectando a Google Photos (abrirá el navegador si es primera vez)...")
client = GooglePhotosClient()
print("✓ Autenticación OK\n")

# Listar álbumes
print("── Álbumes disponibles ──")
albums = client.list_albums()
if not albums:
    print("  (sin álbumes o no hay fotos organizadas en álbumes)")
else:
    for a in albums:
        print(f"  ID: {a['id']}")
        print(f"  Nombre: {a.get('title', '?')}")
        print(f"  Fotos: {a.get('mediaItemsCount', '?')}")
        print()

# Listar últimas 10 fotos
print("── Últimas 10 fotos (últimos 7 días) ──")
from datetime import datetime, timedelta, timezone
since = datetime.now(timezone.utc) - timedelta(days=7)
fotos = client.list_photos_since(since, page_size=10)

if not fotos:
    print("  Sin fotos en los últimos 7 días.")
else:
    for f in fotos[:10]:
        meta = f.get("mediaMetadata", {})
        print(f"  {f.get('filename','?')}  |  {meta.get('creationTime','?')[:10]}")

print("\n✓ Test completado.")
print("\nPróximo paso:")
if albums:
    print(f"  Si tienes un álbum de recibos, copia su ID arriba y úsalo con:")
    print(f"  python main.py --only-photos --photos-album <ID>")
else:
    print("  python main.py --only-photos   (buscará recibos en los últimos 30 días)")
