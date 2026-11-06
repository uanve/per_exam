import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pymupdf

from app import BASE, create_app
from exams import ImportProblem, extract_key, grade, parse_pdf, subject_for
from storage import SCHEMA, connect, initialize_database, load_questions


CONTENT = (BASE / "pdfs" / "2026-07__Julio 2026 - Modelo 3.pdf").read_bytes()
EXPECTED_KEY = "bbccbbccbdbcddcbdddccabadbacadbbbcddcbbacdacc"


class ParserTests(unittest.TestCase):
    def test_missing_solution_is_excluded_but_partial_multiple_is_rejected(self):
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            page = doc[0]
            red = [
                drawing for drawing in page.get_drawings()
                if drawing.get("fill") == (1.0, 0.0, 0.0)
            ]
            self.assertEqual(len(red), 45)
            wrapped = Mock(wraps=page)
            wrapped.get_drawings.return_value = red[1:]
            key = extract_key(wrapped)
            self.assertIsNone(key[1][0])
            self.assertIn("Sin solución", key[1][1])
            first = red[0]
            extra = dict(first)
            rect = first["rect"]
            extra["rect"] = pymupdf.Rect(rect.x0 + 16, rect.y0, rect.x1 + 16, rect.y1)
            wrapped.get_drawings.return_value = [*red, extra]
            with self.assertRaisesRegex(ImportProblem, "ambiguas"):
                extract_key(wrapped)

    def test_all_bundled_pdfs(self):
        expected_exclusions = {
            "2026-02__Febrero 2026 - Modelo 2.pdf": [45],
            "2026-03__Marzo 2026 - Modelo 1.pdf": [17],
            "2026-04__Abril 2026 - Modelo 1.pdf": [39],
        }
        for path in sorted((BASE / "pdfs").glob("2026-*.pdf")):
            with self.subTest(pdf=path.name):
                questions = parse_pdf(path.read_bytes())
                self.assertEqual([q.number for q in questions], list(range(1, 46)))
                self.assertEqual(
                    [q.number for q in questions if q.excluded_reason],
                    expected_exclusions.get(path.name, []),
                )
                for question in questions:
                    if question.excluded_reason:
                        self.assertIsNone(question.correct)
                    else:
                        self.assertIn(question.correct, "abcd")
                        self.assertTrue(all(question.options.get(letter) for letter in "abcd"))
                        self.assertNotRegex(question.text, "Patrón/ona")
        # These marked cells have no extractable letter in the source.
        march = parse_pdf((BASE / "pdfs" / "2026-03__Marzo 2026 - Modelo 2.pdf").read_bytes())
        self.assertEqual(march[19].correct, "d")
        self.assertEqual(march[21].correct, "b")
        self.assertEqual(march[44].correct, "c")

    def test_real_pdf(self):
        questions = parse_pdf(CONTENT)
        self.assertEqual(len(questions), 45)
        self.assertEqual("".join(q.correct for q in questions), EXPECTED_KEY)
        self.assertEqual(sum(len(q.images) for q in questions), 9)
        self.assertEqual([q.number for q in questions if q.images], [3, 5, 8, 18, 19, 20, 21, 23, 26])
        self.assertIn("¿Cómo se denomina", questions[0].text)
        self.assertEqual(questions[0].options["b"], "Filar. / Filar.")
        self.assertIn("sotavento", questions[1].text)
        self.assertEqual(set(questions[44].options), set("abcd"))
        for question in questions:
            for image in question.images:
                self.assertTrue(image.startswith(b"\x89PNG"))
            self.assertNotRegex(question.text, r"\bUT\d+")
            for option in question.options.values():
                self.assertNotRegex(option, r"\bUT\d+")

    def test_reject_invalid_files(self):
        for content in (b"not a pdf", b"%PDF-damaged"):
            with self.subTest(content=content), self.assertRaises(ImportProblem):
                parse_pdf(content)

    def test_reject_missing_key_or_questions(self):
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            doc.delete_page(0)
            with self.assertRaises(ImportProblem):
                parse_pdf(doc.tobytes())
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            doc.delete_page(len(doc) - 1)
            with self.assertRaisesRegex(ImportProblem, "preguntas"):
                parse_pdf(doc.tobytes())

    def test_reject_incomplete_options(self):
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            page = doc[1]
            rect = page.search_for("d) Virar. / Virar.")[0]
            page.add_redact_annot(rect)
            page.apply_redactions()
            with self.assertRaisesRegex(ImportProblem, "cuatro opciones"):
                parse_pdf(doc.tobytes())


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.questions = [{"number": n, "correct": "a"} for n in range(1, 46)]
        self.answers = {str(n): "a" for n in range(1, 46)}

    def mark_wrong(self, numbers):
        for n in numbers:
            self.answers[str(n)] = "b"

    def test_per_exact_pass_threshold(self):
        self.mark_wrong([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 28])
        result = grade(self.questions, self.answers, "per")
        self.assertEqual(result["score"], 32)
        self.assertTrue(result["passed"])
        self.mark_wrong([29])
        self.assertFalse(grade(self.questions, self.answers, "per")["passed"])

    def test_subject_limits_even_with_high_score(self):
        for numbers, subject in (([13, 14, 15], "Balizamiento"), (list(range(18, 24)), "RIPA"), ([42, 43, 44], "Carta")):
            with self.subTest(subject=subject):
                answers = dict(self.answers)
                for n in numbers:
                    answers[str(n)] = "b"
                result = grade(self.questions, answers, "per")
                self.assertFalse(result["passed"])
                self.assertTrue(any(subject in message for message in result["failures"]))

    def test_subject_exact_limits_pass(self):
        self.mark_wrong([13, 14, 18, 19, 20, 21, 22, 42, 43])
        self.assertTrue(grade(self.questions, self.answers, "per")["passed"])

    def test_extension_threshold_and_chart(self):
        self.mark_wrong(range(1, 28))
        self.mark_wrong([28, 42, 43])
        result = grade(self.questions, self.answers, "extension")
        self.assertEqual(result["score"], 15)
        self.assertEqual(result["total"], 18)
        self.assertTrue(result["passed"])
        self.mark_wrong([29])
        self.assertFalse(grade(self.questions, self.answers, "extension")["passed"])
        self.answers["28"] = self.answers["29"] = "a"
        self.mark_wrong([44])
        self.assertFalse(grade(self.questions, self.answers, "extension")["passed"])

    def test_blank_counts_as_error(self):
        result = grade(self.questions, {}, "per")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["blank"], 45)
        self.assertEqual(result["units"][4]["errors"], 5)
        self.assertFalse(result["passed"])

    def test_invalid_answer_and_missing_questions(self):
        with self.assertRaises(ValueError):
            grade(self.questions, {"1": "x"}, "per")
        with self.assertRaises(ValueError):
            grade(self.questions[:40], self.answers, "per")

    def test_subject_boundaries(self):
        self.assertEqual(subject_for(27)[0], 6)
        self.assertEqual(subject_for(28)[0], 7)
        self.assertEqual(subject_for(41)[0], 10)
        self.assertEqual(subject_for(42)[0], 11)

    def test_cancellation_keeps_original_minimum_and_ignores_answers(self):
        self.questions[44].update(correct=None, excluded_reason="Anulada")
        self.mark_wrong([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        self.answers["45"] = "invalid-but-excluded"
        result = grade(self.questions, self.answers, "per")
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 32)
        self.assertEqual(result["total"], 44)
        self.assertEqual(result["units"][-1]["total"], 3)
        self.mark_wrong([28])
        self.assertFalse(grade(self.questions, self.answers, "per")["passed"])
        self.answers = {str(n): "a" for n in range(1, 46)}
        self.mark_wrong([28, 29])
        result = grade(self.questions, self.answers, "extension")
        self.assertEqual(result["score"], 15)
        self.assertEqual(result["total"], 17)
        self.assertTrue(result["passed"])
        self.mark_wrong([30])
        self.assertFalse(grade(self.questions, self.answers, "extension")["passed"])

    def test_cancelled_balizamiento_is_not_an_error_or_blank(self):
        self.questions[16].update(correct=None, excluded_reason="Anulada")
        self.answers.pop("17")
        self.mark_wrong([13, 14])
        result = grade(self.questions, self.answers, "per")
        self.assertTrue(result["passed"])
        self.assertEqual(result["blank"], 0)
        self.assertEqual(result["units"][4]["errors"], 2)
        self.assertEqual(result["units"][4]["total"], 4)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "TESTING": True, "DATA_DIR": self.temp.name,
            "SECRET_KEY": "test-key-not-for-production", "ADMIN_PASSWORD": "test-password",
        }
        seed_dir = Path(self.temp.name) / "pdfs"
        seed_dir.mkdir()
        (seed_dir / "2026-07__Julio 2026 - Modelo 3.pdf").write_bytes(CONTENT)
        self.config["SEED_DIR"] = seed_dir
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def token(self, path="/exams/1"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["csrf"]

    def login(self):
        token = self.token("/admin/login")
        response = self.client.post("/admin/login", data={
            "password": "test-password", "csrf_token": token,
        })
        self.assertEqual(response.status_code, 302)
        return self.token("/admin")

    def modified_pdf(self):
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            doc.set_metadata({"title": "Another exam"})
            return doc.tobytes()

    def upload(self, content, token, title="Noviembre 2026"):
        return self.client.post("/admin", data={
            "csrf_token": token, "title": title, "exam_date": "2026-11-06",
            "pdf": (BytesIO(content), "exam.pdf"),
        })

    def test_catalog_filters_and_health(self):
        self.assertIn(b"Modelo 3", self.client.get("/").data)
        self.assertIn(b"Modelo 3", self.client.get("/?month=2026-07&q=Modelo").data)
        self.assertNotIn(b"Modelo 3", self.client.get("/?month=2026-08").data)
        self.assertNotIn(b"Modelo 3", self.client.get("/?q=unknown").data)
        self.assertEqual(self.client.get("/health").json, {"status": "ok"})
        self.assertEqual(self.client.get("/?mode=bad").status_code, 400)

    def test_digital_exam_and_grading(self):
        response = self.client.get("/exams/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'type="radio"'), 180)
        self.assertNotIn(b"Respuesta correcta", response.data)
        self.assertNotIn(b"data-correct", response.data)
        token = self.token("/exams/1")
        answers = {"q" + str(i): letter for i, letter in enumerate(EXPECTED_KEY, 1)}
        response = self.client.post("/exams/1", data={**answers, "csrf_token": token})
        self.assertEqual(response.status_code, 200)
        self.assertIn("¡Has aprobado!".encode(), response.data)
        self.assertIn(b"45 <span>/ 45", response.data)
        self.assertIn(b"Respuesta correcta", response.data)
        response = self.client.post("/exams/1", data={"csrf_token": token})
        self.assertIn(b"0 <span>/ 45", response.data)
        self.assertIn(b"45 en blanco", response.data)

    def test_extension(self):
        response = self.client.get("/exams/1?mode=extension")
        self.assertEqual(response.data.count(b'type="radio"'), 72)
        self.assertNotIn(b'name="q27"', response.data)
        self.assertIn(b'name="q28"', response.data)
        token = self.token("/exams/1?mode=extension")
        answers = {"q" + str(i): EXPECTED_KEY[i - 1] for i in range(28, 46)}
        response = self.client.post("/exams/1?mode=extension", data={
            **answers, "csrf_token": token,
        })
        self.assertIn(b"18 <span>/ 18", response.data)

    def test_csrf_auth_invalid_options_and_images(self):
        self.assertEqual(self.client.post("/exams/1").status_code, 400)
        self.assertEqual(self.client.get("/admin").status_code, 302)
        token = self.token()
        self.assertEqual(self.client.post("/exams/1", data={
            "csrf_token": token, "q1": "<script>",
        }).status_code, 400)
        self.assertEqual(self.client.post("/exams/1", data={
            "csrf_token": "sesión-no-válida",
        }).status_code, 400)
        self.assertEqual(self.client.post("/exams/1", data={
            "csrf_token": token, "q1": ["a", "b"],
        }).status_code, 400)
        image = self.client.get("/images/1/3/0")
        self.assertEqual(image.mimetype, "image/png")
        self.assertTrue(image.data.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get("/images/1/1/0").status_code, 404)
        self.assertEqual(self.client.get("/exams/999").status_code, 404)

    def test_import_review_publish_and_persistence(self):
        token = self.login()
        response = self.upload(self.modified_pdf(), token)
        self.assertEqual(response.status_code, 302)
        preview = response.headers["Location"]
        exam_id = int(preview.rsplit("/", 1)[1])
        self.assertIn(b"Soluci", self.client.get(preview).data)
        public = self.app.test_client()
        self.assertNotIn(b"Noviembre", public.get("/").data)
        self.assertEqual(public.get(f"/exams/{exam_id}").status_code, 404)
        self.assertEqual(public.get(f"/images/{exam_id}/3/0").status_code, 404)
        self.assertEqual(public.get(preview).status_code, 302)
        response = self.client.post(preview + "/publish", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Noviembre", public.get("/?month=2026-11").data)
        self.assertEqual(public.get(f"/exams/{exam_id}").status_code, 200)
        restarted = create_app(self.config)
        self.assertIn(b"Noviembre", restarted.test_client().get("/").data)
        with closing(connect(Path(self.temp.name) / "exams.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 2)
            self.assertEqual(len(load_questions(db, exam_id)), 45)

    def test_duplicate_invalid_upload_and_discard(self):
        token = self.login()
        self.assertEqual(self.upload(CONTENT, token).status_code, 400)
        self.assertEqual(self.upload(b"fake pdf", token).status_code, 400)
        response = self.upload(self.modified_pdf(), token, "<script>alert(1)</script>")
        preview = response.headers["Location"]
        self.assertNotIn(b"<script>alert(1)</script>", self.client.get(preview).data)
        exam_id = int(preview.rsplit("/", 1)[1])
        self.assertEqual(self.client.post(preview + "/discard", data={
            "csrf_token": token,
        }).status_code, 302)
        self.assertEqual(self.client.get(preview).status_code, 404)
        with closing(connect(Path(self.temp.name) / "exams.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM questions WHERE exam_id=?", (exam_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM images WHERE exam_id=?", (exam_id,)).fetchone()[0], 0)
        self.assertEqual(self.client.post("/admin/exams/1/discard", data={
            "csrf_token": token,
        }).status_code, 400)

    def test_login_limit_and_logout(self):
        token = self.token("/admin/login")
        for _ in range(10):
            self.assertEqual(self.client.post("/admin/login", data={
                "csrf_token": token, "password": "bad",
            }).status_code, 401)
        self.assertEqual(self.client.post("/admin/login", data={
            "csrf_token": token, "password": "test-password",
        }).status_code, 429)

    def test_logout_and_disabled_admin(self):
        token = self.login()
        self.assertEqual(self.client.post("/admin/logout", data={
            "csrf_token": token,
        }).status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.app.config["ADMIN_PASSWORD"] = ""
        self.assertEqual(self.client.get("/admin/login").status_code, 503)

    def test_upload_size_limit(self):
        token = self.login()
        self.app.config["MAX_CONTENT_LENGTH"] = 1024
        response = self.upload(b"%PDF-" + b"x" * 2048, token)
        self.assertEqual(response.status_code, 413)

    def test_cancelled_question_import_and_web_scoring(self):
        token = self.login()
        content = (BASE / "pdfs" / "2026-02__Febrero 2026 - Modelo 2.pdf").read_bytes()
        response = self.upload(content, token)
        preview = response.headers["Location"]
        self.assertIn(b"Anulada", self.client.get(preview).data)
        self.client.post(preview + "/publish", data={"csrf_token": token})
        exam_id = int(preview.rsplit("/", 1)[1])
        response = self.client.get(f"/exams/{exam_id}?mode=extension")
        self.assertEqual(response.data.count(b'type="radio"'), 68)
        self.assertNotIn(b'name="q45"', response.data)
        self.assertIn(b"Anulada", response.data)
        catalogue = self.client.get("/?mode=extension").data
        self.assertIn(b"17 preguntas", catalogue)
        with closing(connect(self.app.config["DATABASE"])) as db:
            questions = load_questions(db, exam_id)
        answers = {"q" + str(q["number"]): q["correct"] for q in questions if q["correct"]}
        response = self.client.post(f"/exams/{exam_id}?mode=extension", data={
            **answers, "csrf_token": token,
        })
        self.assertIn(b"17 <span>/ 17", response.data)
        self.assertIn(b"0 en blanco", response.data)
        self.assertIn(b"Anulada", response.data)


class SeedTests(unittest.TestCase):
    def test_all_bundled_seeds_start_and_restart_without_duplicates(self):
        config = {**self.config, "SEED_DIR": BASE / "pdfs"}
        with self.assertLogs("app", level="WARNING"):
            app = create_app(config)
        with self.assertLogs("app", level="ERROR"):
            create_app(config)
        with closing(connect(app.config["DATABASE"])) as db:
            imported = db.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
            failures = db.execute("SELECT COUNT(*) FROM seed_import_errors").fetchone()[0]
            self.assertEqual(imported + failures, len(list((BASE / "pdfs").glob("*.pdf"))))
            self.assertGreater(db.execute("SELECT COUNT(*) FROM exams WHERE published=1").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM questions").fetchone()[0], imported * 45)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM questions WHERE needs_review=1").fetchone()[0], 0)
            self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(app.test_client().get("/").status_code, 200)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pdfs = root / "pdfs"
        self.pdfs.mkdir()
        self.config = {
            "TESTING": True, "SECRET_KEY": "seed-test-key",
            "DATA_DIR": root / "data", "SEED_DIR": self.pdfs,
        }

    def rows(self):
        with closing(connect(Path(self.config["DATA_DIR"]) / "exams.sqlite3")) as db:
            return [dict(row) for row in db.execute("SELECT * FROM exams ORDER BY id")]

    def test_seed_on_empty_and_existing_database(self):
        (self.pdfs / "2026-07__Julio.pdf").write_bytes(CONTENT)
        first_app = create_app(self.config)
        self.assertIn(b"Julio", first_app.test_client().get("/").data)
        self.assertEqual(self.rows()[0]["exam_date"], "2026-07")
        self.assertEqual(self.rows()[0]["published"], 1)
        (self.pdfs / "2026-08__Copia.PDF").write_bytes(CONTENT)
        with pymupdf.open(stream=CONTENT, filetype="pdf") as doc:
            doc.set_metadata({"title": "New seed"})
            (self.pdfs / "2026-11-06__Noviembre.PDF").write_bytes(doc.tobytes())
        create_app(self.config)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["title"], "Noviembre")
        self.assertEqual(rows[1]["exam_date"], "2026-11-06")
        create_app(self.config)
        self.assertEqual(len(self.rows()), 2)
        (self.pdfs / "2026-07__Julio.pdf").unlink()
        create_app(self.config)
        self.assertEqual(len(self.rows()), 2)

    def test_existing_draft_is_not_published_by_seed(self):
        (self.pdfs / "2026-07__Julio.pdf").write_bytes(CONTENT)
        create_app(self.config)
        with closing(connect(Path(self.config["DATA_DIR"]) / "exams.sqlite3")) as db, db:
            db.execute("UPDATE exams SET published = 0, title = 'Revisar'")
        create_app(self.config)
        self.assertEqual(self.rows()[0]["published"], 0)
        self.assertEqual(self.rows()[0]["title"], "Revisar")

    def test_empty_directory_and_non_pdf_files(self):
        (self.pdfs / "ignore.txt").write_text("not an exam")
        create_app(self.config)
        self.assertEqual(self.rows(), [])

    def test_invalid_names_dates_and_documents_fail_explicitly(self):
        for filename, content in (
            ("exam.pdf", CONTENT),
            ("2026-13__Bad month.pdf", CONTENT),
            ("2026-02-30__Bad day.pdf", CONTENT),
            ("2026-07__Broken.pdf", b"not a pdf"),
        ):
            with self.subTest(filename=filename):
                path = self.pdfs / filename
                path.write_bytes(content)
                with self.assertLogs("app", level="ERROR") as logs:
                    app = create_app(self.config)
                self.assertIn(filename, logs.output[0])
                self.assertEqual(self.rows(), [])
                self.assertEqual(app.test_client().get("/health").status_code, 200)
                path.unlink()

    def test_missing_directory_and_oversize(self):
        with self.assertLogs("app", level="ERROR"):
            create_app({**self.config, "SEED_DIR": self.pdfs / "missing"})
        (self.pdfs / "2026-07__Large.pdf").write_bytes(CONTENT)
        with self.assertLogs("app", level="ERROR"):
            create_app({**self.config, "MAX_CONTENT_LENGTH": 1024})


class MigrationTests(unittest.TestCase):
    def test_legacy_questions_and_images_survive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(connect(Path(directory) / "db.sqlite3")) as db:
                db.execute("""
                    CREATE TABLE questions (
                        exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
                        number INTEGER NOT NULL, text TEXT NOT NULL, options TEXT NOT NULL,
                        correct TEXT NOT NULL CHECK(correct IN ('a','b','c','d')),
                        PRIMARY KEY(exam_id, number)
                    )
                """)
                db.executescript(SCHEMA)
                with db:
                    db.execute("INSERT INTO exams(id,title,exam_date,digest) VALUES(1,'Old','2026-07','old')")
                    db.execute("INSERT INTO questions VALUES(1,1,'Original','{}','b')")
                    db.execute("INSERT INTO images VALUES(1,1,0,?)", (b"original-image",))
                initialize_database(db)
                initialize_database(db)
                row = db.execute("SELECT * FROM questions").fetchone()
                self.assertEqual(row["correct"], "b")
                self.assertEqual(row["excluded_reason"], "")
                self.assertEqual(db.execute("SELECT content FROM images").fetchone()[0], b"original-image")
                with db:
                    db.execute("INSERT INTO questions VALUES(1,2,'Cancelled','{}',NULL,'Anulada',0)")
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())
                with db:
                    db.execute("DELETE FROM exams WHERE id=1")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
