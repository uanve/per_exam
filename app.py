import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from exams import ImportProblem, MODES, grade, parse_pdf, subject_for
from storage import SCHEMA, connect, insert_exam, load_questions


BASE = Path(__file__).resolve().parent


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=os.environ.get("DATA_DIR", str(BASE / "data")),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        MAX_CONTENT_LENGTH=15 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RAILWAY_ENVIRONMENT_ID") is not None,
        PERMANENT_SESSION_LIFETIME=3600,
        SEED_EXAM=True,
        SEED_DIR=BASE / "pdfs",
    )
    if test_config:
        app.config.update(test_config)
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config["DATABASE"] = data_dir / "exams.sqlite3"
    if not app.config["SECRET_KEY"]:
        if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
            raise RuntimeError("Configura SECRET_KEY en Railway antes de arrancar la aplicación.")
        secret_path = data_dir / "session.key"
        try:
            with secret_path.open("x", encoding="ascii") as file:
                secret_path.chmod(0o600)
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = secret_path.read_text(encoding="ascii").strip()
    if os.environ.get("TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    with closing(connect(app.config["DATABASE"])) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        if app.config["SEED_EXAM"]:
            seed_dir = Path(app.config["SEED_DIR"])
            if not seed_dir.is_dir():
                raise ImportProblem(f"No existe la carpeta de exámenes iniciales: {seed_dir}")
            for path in sorted(seed_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() != ".pdf":
                    continue
                try:
                    match = re.fullmatch(r"(\d{4}-\d{2}(?:-\d{2})?)__(.+)", path.stem)
                    if not match:
                        raise ImportProblem("Usa el nombre AAAA-MM__Titulo.pdf o AAAA-MM-DD__Titulo.pdf.")
                    exam_date, title = match.groups()
                    title = title.strip()
                    try:
                        date.fromisoformat(exam_date + "-01" if len(exam_date) == 7 else exam_date)
                    except ValueError as error:
                        raise ImportProblem("La fecha del nombre del PDF no es válida.") from error
                    if not 1 <= len(title) <= 150:
                        raise ImportProblem("El título debe tener entre 1 y 150 caracteres.")
                    if path.stat().st_size > app.config["MAX_CONTENT_LENGTH"]:
                        raise ImportProblem("El PDF supera el límite de 15 MB.")
                    content = path.read_bytes()
                    digest = hashlib.sha256(content).hexdigest()
                    if db.execute("SELECT id FROM exams WHERE digest = ?", (digest,)).fetchone():
                        continue
                    insert_exam(db, content, parse_pdf(content), title, exam_date, published=True)
                    app.logger.info("Examen inicial importado y publicado: %s", path.name)
                except (ImportProblem, OSError) as error:
                    message = f"No se pudo importar el examen inicial {path.name}: {error}"
                    app.logger.error("%s", message)
                    raise ImportProblem(message) from error

    def database() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_database(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        return session["csrf"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                abort(400, description="La sesión ha caducado. Recarga la página y vuelve a intentarlo.")

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def shared_context():
        return {"csrf_token": csrf_token, "modes": MODES, "subject_for": subject_for}

    def admin_required(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if not session.get("admin"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return guarded

    def get_exam(exam_id: int, allow_draft: bool = False):
        exam = database().execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        if exam is None or (not exam["published"] and not allow_draft):
            abort(404, description="No se ha encontrado el examen.")
        return exam

    def selected_mode() -> str:
        mode = request.args.get("mode", "per")
        if mode not in MODES:
            abort(400, description="Modalidad no válida.")
        return mode

    @app.get("/health")
    def health():
        database().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/")
    def index():
        mode = selected_mode()
        month = request.args.get("month", "").strip()
        query = request.args.get("q", "").strip()[:100]
        exams = database().execute(
            "SELECT * FROM exams WHERE published = 1 AND exam_date LIKE ? "
            "AND title LIKE ? ORDER BY exam_date DESC, id DESC",
            (month + "%", "%" + query + "%"),
        ).fetchall()
        return render_template("index.html", exams=exams, mode=mode, month=month, query=query)

    @app.route("/exams/<int:exam_id>", methods=["GET", "POST"])
    def exam_page(exam_id):
        exam = get_exam(exam_id)
        mode = selected_mode()
        questions = load_questions(database(), exam_id)
        questions = [q for q in questions if q["number"] >= MODES[mode]["first"]]
        if request.method == "POST":
            answers = {}
            for question in questions:
                name = str(question["number"])
                values = request.form.getlist("q" + name)
                if len(values) > 1:
                    abort(400, description="Solo se admite una respuesta por pregunta.")
                answers[name] = values[0] if values else ""
            try:
                result = grade(questions, answers, mode)
            except ValueError as error:
                abort(400, description=str(error))
            return render_template("result.html", exam=exam, mode=mode, result=result)
        return render_template("exam.html", exam=exam, mode=mode, questions=questions)

    @app.get("/images/<int:exam_id>/<int:number>/<int:position>")
    def image(exam_id, number, position):
        get_exam(exam_id, allow_draft=bool(session.get("admin")))
        row = database().execute(
            "SELECT content FROM images WHERE exam_id = ? AND number = ? AND position = ?",
            (exam_id, number, position),
        ).fetchone()
        if row is None:
            abort(404, description="No se ha encontrado la imagen.")
        return Response(row["content"], mimetype="image/png")

    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        if not app.config["ADMIN_PASSWORD"]:
            abort(503, description="La administración está desactivada. Configura ADMIN_PASSWORD en el servidor.")
        if request.method == "POST":
            db = database()
            address = request.remote_addr or "unknown"
            now = time.time()
            with db:
                db.execute("DELETE FROM login_attempts WHERE started < ?", (now - 900,))
                db.execute(
                    "INSERT INTO login_attempts VALUES (?, 1, ?) "
                    "ON CONFLICT(address) DO UPDATE SET failures = failures + 1",
                    (address, now),
                )
                attempt = db.execute(
                    "SELECT failures FROM login_attempts WHERE address = ?", (address,)
                ).fetchone()
            if attempt["failures"] > 10:
                abort(429, description="Demasiados intentos. Espera 15 minutos antes de volver a intentarlo.")
            if hmac.compare_digest(
                request.form.get("password", "").encode(), app.config["ADMIN_PASSWORD"].encode()
            ):
                with db:
                    db.execute("DELETE FROM login_attempts WHERE address = ?", (address,))
                session.clear()
                session["admin"] = True
                session.permanent = True
                return redirect(url_for("admin"))
            flash("Contraseña incorrecta.", "error")
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.post("/admin/logout")
    @admin_required
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/admin", methods=["GET", "POST"])
    @admin_required
    def admin():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            exam_date = request.form.get("exam_date", "").strip()
            uploaded = request.files.get("pdf")
            try:
                if not 1 <= len(title) <= 150:
                    raise ImportProblem("Escribe un título de entre 1 y 150 caracteres.")
                try:
                    date.fromisoformat(exam_date)
                except ValueError as error:
                    raise ImportProblem("Selecciona una fecha válida para la convocatoria.") from error
                if uploaded is None or not uploaded.filename:
                    raise ImportProblem("Selecciona el PDF del examen.")
                content = uploaded.read()
                digest = hashlib.sha256(content).hexdigest()
                if database().execute("SELECT id FROM exams WHERE digest = ?", (digest,)).fetchone():
                    raise ImportProblem("Este PDF ya está importado. Revisa los exámenes y borradores existentes.")
                questions = parse_pdf(content)
                try:
                    exam_id = insert_exam(database(), content, questions, title, exam_date)
                except sqlite3.IntegrityError as error:
                    raise ImportProblem("Este PDF ya se ha importado en otra petición.") from error
                return redirect(url_for("preview", exam_id=exam_id))
            except ImportProblem as error:
                app.logger.warning("Importación rechazada: %s", error)
                flash(str(error), "error")
                exams = database().execute("SELECT * FROM exams ORDER BY exam_date DESC, id DESC").fetchall()
                return render_template("admin.html", exams=exams), 400
        exams = database().execute("SELECT * FROM exams ORDER BY exam_date DESC, id DESC").fetchall()
        return render_template("admin.html", exams=exams)

    @app.get("/admin/exams/<int:exam_id>")
    @admin_required
    def preview(exam_id):
        exam = get_exam(exam_id, allow_draft=True)
        return render_template(
            "preview.html", exam=exam, questions=load_questions(database(), exam_id),
        )

    @app.post("/admin/exams/<int:exam_id>/publish")
    @admin_required
    def publish(exam_id):
        get_exam(exam_id, allow_draft=True)
        with database() as db:
            db.execute("UPDATE exams SET published = 1 WHERE id = ?", (exam_id,))
        flash("Examen publicado. Ya está disponible para los alumnos.", "success")
        return redirect(url_for("admin"))

    @app.post("/admin/exams/<int:exam_id>/discard")
    @admin_required
    def discard(exam_id):
        exam = get_exam(exam_id, allow_draft=True)
        if exam["published"]:
            abort(400, description="Solo se pueden descartar borradores.")
        with database() as db:
            db.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
        flash("Borrador descartado.", "success")
        return redirect(url_for("admin"))

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template("error.html", code=413, message="El PDF supera el límite de 15 MB."), 413

    for code in (400, 404, 429, 503):
        def handle_error(error):
            return render_template("error.html", code=error.code, message=error.description), error.code
        app.register_error_handler(code, handle_error)

    return app
