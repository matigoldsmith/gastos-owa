# Expense Tracker

Sistema de seguimiento de gastos para empresa de inversiones.
Lee Gmail automáticamente, extrae datos con Claude y clasifica con reglas aprendidas.

## Arquitectura

```
Gmail → main.py (pipeline) → gastos.db (SQLite) → web_app.py (Flask)
```

## Setup inicial

### 1. Instalar dependencias

```bash
cd "Gastos OWA"
python -m venv venv
source venv/bin/activate        # macOS/Linux
pip install -r requirements.txt
```

### 2. Variables de entorno

```bash
cp .env.example .env
# Editar .env con tu ANTHROPIC_API_KEY y ajustes
```

### 3. Configurar Gmail OAuth2

1. Ir a [Google Cloud Console](https://console.cloud.google.com/)
2. Crear proyecto → Habilitar **Gmail API**
3. Configurar pantalla OAuth (tipo: externa, usuario de prueba: tu email)
4. Crear credencial → **OAuth 2.0 → Aplicación de escritorio**
5. Descargar JSON → guardarlo como `credentials.json` en esta carpeta
6. Primer uso: `python main.py` abrirá el navegador para autorizar acceso

El token se guarda en `token.json` y se renueva automáticamente.

### 4. Correr el sistema

**Una vez (procesar emails nuevos y salir):**
```bash
python main.py
```

**Loop continuo (revisar cada 30 min):**
```bash
python main.py --loop
```

**Web app (interfaz de revisión):**
```bash
python web_app.py
# Abrir http://127.0.0.1:5000
```

## Flujo de trabajo

1. `main.py` lee Gmail → extrae datos con Claude → clasifica con reglas → guarda en SQLite
2. Abrir `http://localhost:5000` para revisar gastos
3. Dar feedback natural: "todo lo de AWS es software" → se crea una regla persistente
4. Los gastos existentes que coinciden se re-clasifican automáticamente

## Estructura de archivos

```
├── schema.sql          Schema SQLite (tablas + categorías por defecto)
├── config.py           Configuración (lee desde .env)
├── gmail_client.py     Cliente Gmail con OAuth2
├── extractor.py        Extracción con Claude API (texto + PDF + imágenes)
├── clasificador.py     Clasificación por reglas y por IA, feedback loop
├── main.py             Orquestador del pipeline
├── web_app.py          Web app Flask
├── templates/          HTML de la web app
├── .env.example        Plantilla de variables de entorno
├── requirements.txt    Dependencias Python
├── gastos.db           Base de datos SQLite (se crea automáticamente)
└── logs/gastos.log     Logs del sistema
```

## Agregar nuevas fuentes de ingesta

El diseño es modular. Para agregar WhatsApp, fotos, etc.:
1. Crear un módulo tipo `whatsapp_client.py` que retorne `email_data`-like dicts
2. Llamar a `extract_batch()` y `clasificar()` desde `main.py` igual que con Gmail
3. Usar `fuente='whatsapp'` en el campo correspondiente al guardar

## Categorías por defecto

- Servicios Financieros
- Software y Tecnología
- Consultoría y Asesoría
- Viajes y Transporte
- Oficina y Suministros
- Marketing y Comunicación
- Alimentación
- Telecomunicaciones
- Impuestos y Tasas
- Otros

Se pueden agregar/modificar directamente en SQLite o via la web app.
