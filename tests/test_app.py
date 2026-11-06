import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pymupdf

from app import BASE, create_app
from exams import ImportProblem, grade, parse_pdf, subject_for
from storage import connect, load_questions


CONTENT = (BASE / "pdfs" / "2026-07__Julio 2026 - Modelo 3.pdf").read_bytes()
EXPECTED_KEY = "bbccbbccbdbcddcbdddccabadbacadbbbcddcbbacdacc"


class ParserTests(unittest.TestCase):
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


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "TESTING": True, "DATA_DIR": self.temp.name,
            "SECRET_KEY": "test-key-not-for-production", "ADMIN_PASSWORD": "test-password",
        }
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


class SeedTests(unittest.TestCase):
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
                    with self.assertRaisesRegex(ImportProblem, filename):
                        create_app(self.config)
                self.assertIn(filename, logs.output[0])
                self.assertEqual(self.rows(), [])
                path.unlink()

    def test_missing_directory_and_oversize(self):
        with self.assertRaisesRegex(ImportProblem, "No existe"):
            create_app({**self.config, "SEED_DIR": self.pdfs / "missing"})
        (self.pdfs / "2026-07__Large.pdf").write_bytes(CONTENT)
        with self.assertLogs("app", level="ERROR"):
            with self.assertRaisesRegex(ImportProblem, "límite"):
                create_app({**self.config, "MAX_CONTENT_LENGTH": 1024})


if __name__ == "__main__":
    unittest.main()
