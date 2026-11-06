import re
from dataclasses import dataclass, field

import pymupdf


SUBJECTS = (
    (1, 4, "Nomenclatura náutica"),
    (2, 6, "Amarre y fondeo"),
    (3, 10, "Seguridad"),
    (4, 12, "Legislación"),
    (5, 17, "Balizamiento"),
    (6, 27, "Reglamento (RIPA)"),
    (7, 29, "Maniobra"),
    (8, 32, "Emergencias en el mar"),
    (9, 36, "Meteorología"),
    (10, 41, "Teoría de navegación"),
    (11, 45, "Carta de navegación"),
)
MODES = {
    "per": {"label": "PER completo", "first": 1, "total": 45, "minimum": 32},
    "extension": {
        "label": "Ampliación desde PNB", "first": 28, "total": 18, "minimum": 15,
    },
}
LIMITS = {5: 2, 6: 5, 11: 2}


class ImportProblem(ValueError):
    pass


@dataclass
class Question:
    number: int
    text: str
    correct: str | None
    options: dict[str, str] = field(default_factory=dict)
    images: list[bytes] = field(default_factory=list)
    excluded_reason: str = ""
    needs_review: bool = False


def subject_for(number: int) -> tuple[int, str]:
    for unit, last, name in SUBJECTS:
        if number <= last:
            return unit, name
    raise ValueError(f"Número de pregunta fuera de rango: {number}")


def extract_key(page: pymupdf.Page) -> dict[int, tuple[str | None, str]]:
    words = page.get_text("words")
    red_cells = [
        drawing["rect"] for drawing in page.get_drawings()
        if drawing.get("fill")
        and drawing["fill"][0] > 0.7
        and drawing["fill"][1] < 0.35
        and drawing["fill"][2] < 0.35
    ]
    number_words = [
        word for word in words if word[4].isdigit() and 1 <= int(word[4]) <= 45
    ]
    if {int(word[4]) for word in number_words} != set(range(1, 46)) or len(red_cells) < 30:
        raise ImportProblem("La primera página no contiene una plantilla de 45 soluciones reconocible.")
    marked: dict[int, set[str]] = {}
    for rect in red_cells:
        center = (rect.tl + rect.br) / 2
        numbers = [
            item for item in number_words
            if item[2] < rect.x0
            and abs((item[1] + item[3]) / 2 - center.y) < 3
        ]
        if not numbers:
            raise ImportProblem("No se pudo asociar una solución roja a su pregunta.")
        number = int(max(numbers, key=lambda item: item[2])[4])
        # Some marked cells cover the letter; other rows establish the column.
        letters = {
            word[4] for word in words
            if word[4] in ("a", "b", "c", "d")
            and abs((word[0] + word[2]) / 2 - center.x) < 2
        }
        if len(letters) != 1:
            raise ImportProblem(f"No se pudo identificar la casilla roja de la pregunta {number}.")
        marked.setdefault(number, set()).update(letters)
    answers = {}
    for number in range(1, 46):
        letters = marked.get(number, set())
        if len(letters) == 1:
            answers[number] = (next(iter(letters)), "")
        elif letters == set("abcd"):
            answers[number] = (None, "Anulada en la plantilla: las cuatro respuestas están marcadas.")
        elif not letters:
            answers[number] = (None, "Sin solución marcada en el PDF original.")
        else:
            raise ImportProblem(f"Soluciones ambiguas para la pregunta {number}: no hay una ni cuatro casillas.")
    return answers


def parse_pdf(content: bytes, *, allow_manual_key: bool = False) -> list[Question]:
    if not content.startswith(b"%PDF-"):
        raise ImportProblem("El archivo no es un PDF válido.")
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as error:
        raise ImportProblem("No se puede abrir el PDF; puede estar dañado.") from error
    with document:
        if document.needs_pass or not 2 <= len(document) <= 40:
            raise ImportProblem("El PDF debe tener entre 2 y 40 páginas y no estar protegido.")
        key_problem = ""
        try:
            answers = extract_key(document[0])
        except ImportProblem as error:
            if not allow_manual_key:
                raise
            key_problem = str(error)
            answers = {number: (None, key_problem) for number in range(1, 46)}
        questions: list[Question] = []
        current: Question | None = None
        option: str | None = None
        for page in list(document)[1:]:
            events = []
            lines = []
            for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    if text:
                        lines.append((line["bbox"][1], line["bbox"][0], text))
            rows = []
            for y, x, text in sorted(lines):
                if rows and abs(rows[-1][0] - y) < 2:
                    rows[-1][1].append((x, text))
                else:
                    rows.append((y, [(x, text)]))
            for y, fragments in rows:
                events.append((y, min(x for x, _ in fragments), " ".join(
                    text for _, text in sorted(fragments)
                )))
            for info in page.get_image_info():
                rect = pymupdf.Rect(info["bbox"]) & page.rect
                if not rect.is_empty:
                    events.append((rect.y0, rect.x0, rect))
            for _, _, event in sorted(events, key=lambda item: (item[0], item[1])):
                if isinstance(event, pymupdf.Rect):
                    if current is not None:
                        current.images.append(
                            page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=event).tobytes("png")
                        )
                    continue
                text = event
                if re.search(r"\bUT\s*\d+", text, re.I) or re.fullmatch(r"\(\d+\s*[-–]\s*\d+\)", text):
                    continue
                if re.search(r"Examen teòric|Patrón/ona de Embarcaci", text) or text in ("•", "x"):
                    continue
                start = re.match(r"^(\d{1,2})\s*(?:[.)]\s*[-–]?|[-–])\s*(.*)", text)
                choice = re.match(r"^([a-dA-D])\s*[).]\s*(.*)", text)
                if start:
                    number = int(start[1])
                    if number < (questions[-1].number if questions else 0) or number not in answers:
                        raise ImportProblem(f"Orden de preguntas inesperado: {number}. Revisa el formato.")
                    correct, reason = answers[number]
                    pending = allow_manual_key and (
                        bool(key_problem) or reason == "Sin solución marcada en el PDF original."
                    )
                    current = Question(
                        number, start[2], correct, excluded_reason=reason,
                        needs_review=pending,
                    )
                    questions.append(current)
                    option = None
                elif choice and current is not None:
                    option = choice[1].lower()
                    if option in current.options:
                        raise ImportProblem(f"Opción duplicada en la pregunta {current.number}.")
                    current.options[option] = choice[2]
                elif current is not None:
                    if option:
                        current.options[option] += " " + text
                    else:
                        current.text += " " + text
        # Only internal gaps are identifiable as missing source questions.
        # A missing final page must still fail, not create a truncated exam.
        if not questions or questions[-1].number != 45:
            raise ImportProblem(f"Se han encontrado {len(questions)} preguntas; falta el final del examen.")
        unique = {}
        for question in questions:
            if question.number in unique:
                previous = unique[question.number]
                if (
                    " ".join(previous.text.split()) != " ".join(question.text.split())
                    or previous.options != question.options or previous.images != question.images
                ):
                    raise ImportProblem(f"La pregunta {question.number} aparece repetida con contenido distinto.")
            unique[question.number] = question
        questions = list(unique.values())
        present = set(unique)
        for number in range(1, 46):
            if number not in present:
                questions.append(Question(
                    number, "Pregunta ausente en el PDF original.", None,
                    excluded_reason="El enunciado y las opciones no aparecen en el PDF original.",
                ))
        questions.sort(key=lambda question: question.number)
        for question in questions:
            if question.excluded_reason and not question.needs_review:
                continue
            if not question.text or set(question.options) != set("abcd") or not all(question.options.values()):
                raise ImportProblem(f"La pregunta {question.number} no tiene enunciado y cuatro opciones completas.")
        return questions


def grade(questions: list[dict], answers: dict[str, str], mode: str) -> dict:
    rules = MODES[mode]
    in_mode = [q for q in questions if q["number"] >= rules["first"]]
    if any(q.get("needs_review") for q in in_mode):
        raise ValueError("El examen tiene soluciones pendientes de revisión.")
    if len(in_mode) != rules["total"]:
        raise ValueError("El examen no tiene el número de preguntas requerido.")
    excluded = [q for q in in_mode if q.get("excluded_reason")]
    selected = [q for q in in_mode if not q.get("excluded_reason")]
    rows = []
    units = {}
    for question in selected:
        number = question["number"]
        answer = answers.get(str(number), "")
        if answer not in ("", "a", "b", "c", "d"):
            raise ValueError(f"Respuesta inválida para la pregunta {number}.")
        correct = answer == question["correct"]
        unit, name = subject_for(number)
        summary = units.setdefault(unit, {
            "name": name, "total": 0, "correct": 0, "errors": 0, "limit": LIMITS.get(unit),
        })
        summary["total"] += 1
        summary["correct"] += int(correct)
        summary["errors"] += int(not correct)
        rows.append({**question, "answer": answer, "is_correct": correct})
    score = sum(row["is_correct"] for row in rows)
    failures = [
        f'{unit["name"]}: {unit["errors"]} errores (máximo {unit["limit"]}).'
        for unit in units.values()
        if unit["limit"] is not None and unit["errors"] > unit["limit"]
    ]
    if score < rules["minimum"]:
        failures.insert(0, f'Se necesitan {rules["minimum"]} aciertos; has conseguido {score}.')
    return {
        "passed": not failures, "score": score, "total": len(rows),
        "blank": sum(not row["answer"] for row in rows),
        "excluded": excluded,
        "failures": failures, "units": list(units.values()), "questions": rows,
    }
