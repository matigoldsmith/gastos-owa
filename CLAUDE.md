# Gastos Owa — Expense Tracker

**Arquitectura:** Gmail → `main.py` → `gastos.db` (SQLite) → `web_app.py` (Flask · `http://localhost:5000`)

## Comandos
```bash
cd "/Users/mgoldsmithd/Scripts Claude AI/Gastos Owa" && source venv/bin/activate

python main.py           # procesar emails nuevos (una vez)
python main.py --loop    # loop continuo cada 30 min
python web_app.py        # web app de revisión
```

## Archivos clave
- `extractor.py` — extracción con Claude API (texto + PDF + imágenes)
- `clasificador.py` — clasificación por reglas + IA, feedback loop
- `gmail_client.py` — Gmail OAuth2 (`credentials.json`, `token.json`)
- `config.py` — lee `.env` (ANTHROPIC_API_KEY + ajustes)
- `web_app.py` — Flask UI; feedback natural → reglas persistentes
- `gastos.db` — SQLite (se crea automáticamente)

## Diseño
- Feedback en lenguaje natural: "todo lo de AWS es software" → crea regla → re-clasifica existentes
- Modular: agregar fuentes (WhatsApp, fotos) como `*_client.py` que retornen `email_data`-like dicts
- Fuentes disponibles: Gmail, Google Photos (`google_photos_client.py`), Dropbox (`dropbox_client.py`)
