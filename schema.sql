-- ============================================================
-- Expense Tracker — Schema SQLite
-- ============================================================

-- Categorías
CREATE TABLE IF NOT EXISTS categorias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre      TEXT NOT NULL UNIQUE,
    descripcion TEXT,
    activa      BOOLEAN DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Gastos
CREATE TABLE IF NOT EXISTS gastos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha        DATE,
    proveedor    TEXT,
    monto        REAL,
    moneda       TEXT DEFAULT 'CLP',
    categoria_id INTEGER REFERENCES categorias(id),
    descripcion  TEXT,
    fuente       TEXT DEFAULT 'gmail',
    email_id     TEXT,
    confianza    REAL DEFAULT 0.0,
    revisado     BOOLEAN DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gastos_fecha     ON gastos(fecha);
CREATE INDEX IF NOT EXISTS idx_gastos_revisado  ON gastos(revisado);
CREATE INDEX IF NOT EXISTS idx_gastos_categoria ON gastos(categoria_id);

-- Reglas aprendidas
CREATE TABLE IF NOT EXISTS reglas (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patron       TEXT NOT NULL,
    categoria_id INTEGER REFERENCES categorias(id),
    tipo         TEXT NOT NULL,  -- 'proveedor_exacto' | 'proveedor_contiene' | 'descripcion_contiene'
    confianza    REAL DEFAULT 1.0,
    usos         INTEGER DEFAULT 0,
    activa       BOOLEAN DEFAULT 1,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reglas_patron_tipo ON reglas(patron, tipo);

-- Feedback del usuario
CREATE TABLE IF NOT EXISTS feedback (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    gasto_id       INTEGER REFERENCES gastos(id),
    feedback_texto TEXT,
    accion_tomada  TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Emails procesados (evita re-procesar)
CREATE TABLE IF NOT EXISTS emails_procesados (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    fecha            TIMESTAMP,
    asunto           TEXT,
    procesado_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resultado        TEXT  -- 'ok' | 'sin_gasto' | 'error'
);

-- Fotos procesadas de Google Photos (evita re-procesar)
CREATE TABLE IF NOT EXISTS fotos_procesadas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photos_media_id TEXT UNIQUE NOT NULL,
    filename        TEXT,
    fecha_foto      TIMESTAMP,
    procesado_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resultado       TEXT  -- 'ok' | 'sin_gasto' | 'error'
);

-- Fotos procesadas de Dropbox Camera Upload (evita re-procesar)
CREATE TABLE IF NOT EXISTS dropbox_procesadas (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dropbox_path TEXT UNIQUE NOT NULL,   -- path_lower único en Dropbox
    filename     TEXT,
    fecha_foto   DATE,
    procesado_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resultado    TEXT  -- 'ok' | 'sin_gasto' | 'error'
);

-- ============================================================
-- Categorías por defecto (empresa de inversiones)
-- ============================================================
INSERT OR IGNORE INTO categorias (nombre, descripcion) VALUES
    ('Servicios Financieros',    'Bancos, corredoras, custodios, asesoría financiera'),
    ('Software y Tecnología',    'SaaS, licencias, hosting, herramientas digitales'),
    ('Consultoría y Asesoría',   'Honorarios profesionales, legal, contabilidad, auditoría'),
    ('Viajes y Transporte',      'Vuelos, hoteles, taxi, combustible, peajes'),
    ('Oficina y Suministros',    'Arriendo oficina, materiales, equipos'),
    ('Marketing y Comunicación', 'Publicidad, diseño, relaciones públicas'),
    ('Alimentación',             'Comidas de trabajo, eventos con clientes'),
    ('Telecomunicaciones',       'Internet, telefonía, comunicaciones'),
    ('Impuestos y Tasas',        'IVA, impuestos, tasas gubernamentales'),
    ('Otros',                    'Gastos no clasificados');
