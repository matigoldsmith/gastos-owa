"""
Web app Flask para revisión de gastos y feedback.
Corre localmente en http://127.0.0.1:5000
"""
import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, url_for

from config import DB_PATH, SECRET_KEY, WEB_HOST, WEB_PORT, ITEMS_PER_PAGE
from clasificador import procesar_feedback, get_categorias

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    """Crea schema si no existe."""
    schema = (Path(__file__).parent / "schema.sql").read_text()
    db = get_db()
    db.executescript(schema)
    db.commit()
    db.close()


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Dashboard con stats resumidas."""
    db = get_db()
    try:
        stats = {
            "total":        db.execute("SELECT COUNT(*) FROM gastos").fetchone()[0],
            "sin_revisar":  db.execute("SELECT COUNT(*) FROM gastos WHERE revisado=0").fetchone()[0],
            "baja_conf":    db.execute("SELECT COUNT(*) FROM gastos WHERE confianza < 0.7 AND revisado=0").fetchone()[0],
            "monto_mes":    db.execute(
                "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE strftime('%Y-%m',fecha)=strftime('%Y-%m','now')"
            ).fetchone()[0],
            "reglas":       db.execute("SELECT COUNT(*) FROM reglas WHERE activa=1").fetchone()[0],
        }
        por_categoria = db.execute("""
            SELECT c.nombre, COUNT(g.id) as n, COALESCE(SUM(g.monto),0) as total
            FROM gastos g
            JOIN categorias c ON g.categoria_id = c.id
            WHERE strftime('%Y-%m',g.fecha) = strftime('%Y-%m','now')
            GROUP BY c.id ORDER BY total DESC
        """).fetchall()
    finally:
        db.close()

    return render_template("index.html", stats=stats, por_categoria=por_categoria)


@app.route("/gastos")
def gastos():
    """Lista de gastos con filtros."""
    cat_id   = request.args.get("cat", type=int)
    revisado = request.args.get("revisado")  # '0', '1', o None
    moneda   = request.args.get("moneda")
    page     = request.args.get("page", 1, type=int)
    offset   = (page - 1) * ITEMS_PER_PAGE

    db = get_db()
    try:
        # Construir WHERE dinámicamente
        filters = []
        params  = []
        if cat_id is not None:
            filters.append("g.categoria_id = ?");  params.append(cat_id)
        if revisado is not None:
            filters.append("g.revisado = ?");       params.append(int(revisado))
        if moneda:
            filters.append("g.moneda = ?");         params.append(moneda)

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        total = db.execute(f"SELECT COUNT(*) FROM gastos g {where}", params).fetchone()[0]
        rows  = db.execute(f"""
            SELECT g.*, c.nombre as categoria_nombre
            FROM gastos g
            LEFT JOIN categorias c ON g.categoria_id = c.id
            {where}
            ORDER BY g.fecha DESC, g.id DESC
            LIMIT ? OFFSET ?
        """, params + [ITEMS_PER_PAGE, offset]).fetchall()

        categorias = db.execute("SELECT id, nombre FROM categorias WHERE activa=1 ORDER BY nombre").fetchall()
    finally:
        db.close()

    total_pages = max(1, -(-total // ITEMS_PER_PAGE))  # ceil division

    return render_template(
        "gastos.html",
        gastos=rows,
        categorias=categorias,
        page=page,
        total_pages=total_pages,
        total=total,
        filtros={"cat": cat_id, "revisado": revisado, "moneda": moneda},
    )


@app.route("/gastos/<int:gasto_id>/revisar", methods=["POST"])
def revisar_gasto(gasto_id: int):
    """Marca un gasto como revisado, opcionalmente cambia categoría."""
    data        = request.get_json(silent=True) or {}
    nueva_cat   = data.get("categoria_id")

    db = get_db()
    try:
        if nueva_cat:
            db.execute(
                "UPDATE gastos SET revisado=1, categoria_id=?, confianza=1.0 WHERE id=?",
                (nueva_cat, gasto_id),
            )
        else:
            db.execute("UPDATE gastos SET revisado=1 WHERE id=?", (gasto_id,))
        db.commit()
    finally:
        db.close()

    return jsonify({"ok": True})


@app.route("/gastos/<int:gasto_id>/eliminar", methods=["POST"])
def eliminar_gasto(gasto_id: int):
    db = get_db()
    try:
        db.execute("DELETE FROM gastos WHERE id=?", (gasto_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/feedback", methods=["POST"])
def feedback():
    """
    Recibe feedback en lenguaje natural.
    Body JSON: {"texto": "...", "gasto_id": null | int}
    """
    data      = request.get_json(silent=True) or {}
    texto     = (data.get("texto") or "").strip()
    gasto_id  = data.get("gasto_id")

    if not texto:
        return jsonify({"ok": False, "error": "Texto vacío"}), 400

    db = get_db()
    try:
        accion = procesar_feedback(db, gasto_id, texto)
    finally:
        db.close()

    return jsonify({"ok": True, "accion": accion})


@app.route("/reglas")
def reglas():
    """Lista de reglas aprendidas."""
    db = get_db()
    try:
        rows = db.execute("""
            SELECT r.*, c.nombre as categoria_nombre
            FROM reglas r
            JOIN categorias c ON r.categoria_id = c.id
            WHERE r.activa = 1
            ORDER BY r.usos DESC, r.created_at DESC
        """).fetchall()
    finally:
        db.close()
    return render_template("reglas.html", reglas=rows)


@app.route("/reglas/<int:regla_id>/desactivar", methods=["POST"])
def desactivar_regla(regla_id: int):
    db = get_db()
    try:
        db.execute("UPDATE reglas SET activa=0 WHERE id=?", (regla_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/categorias")
def api_categorias():
    db = get_db()
    try:
        cats = [dict(r) for r in get_categorias(db)]
    finally:
        db.close()
    return jsonify(cats)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"\n🚀 Expense Tracker corriendo en http://{WEB_HOST}:{WEB_PORT}\n")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
