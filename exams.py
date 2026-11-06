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
    correct: str
    options: dict[str, str] = field(default_factory=dict)
    images: list[bytes] = field(default_factory=list)


def subject_for(number: int) -> tuple[int, str]:
    for unit, last, name in SUBJECTS:
        if number <= last:
            return unit, name
    raise ValueError(f"Número de pregunta fuera de rango: {number}")


def extract_key(page: pymupdf.Page) -> dict[int, str]:
    words = page.get_text("words")
    red_cells = [
        drawing["rect"] for drawing in page.get_drawings()
        if drawing.get("fill")
        and drawing["fill"][0] > 0.7
        and drawing["fill"][1] < 0.35
        and drawing["fill"][2] < 0.35
    ]
    answers: dict[int, str] = {}
    for word in words:
        letter = word[4].strip().lower()
        if letter not in ("a", "b", "c", "d"):
            continue
        center = pymupdf.Point((word[0] + word[2]) / 2, (word[1] + word[3]) / 2)
        if not any(center in rect for rect in red_cells):
            continue
        numbers = [
            item for item in words
            if item[4].isdigit() and 1 <= int(item[4]) <= 45
            and item[2] < word[0]
            and abs((item[1] + item[3]) / 2 - center.y) < 3
        ]
        if not numbers:
            raise ImportProblem("No se pudo asociar una solución roja a su pregunta.")
        number = int(max(numbers, key=lambda item: item[2])[4])
        if number in answers:
            raise ImportProblem(f"Hay varias soluciones marcadas para la pregunta {number}.")
        answers[number] = letter
    if set(answers) != set(range(1, 46)):
        raise ImportProblem(
            "La primera página debe contener las 45 soluciones, con una casilla "
            "roja por pregunta, como en el PDF de ejemplo. No se ha importado el examen."
        )
    return answers


def parse_pdf(content: bytes) -> list[Question]:
    if not content.startswith(b"%PDF-"):
        raise ImportProblem("El archivo no es un PDF válido.")
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as error:
        raise ImportProblem("No se puede abrir el PDF; puede estar dañado.") from error
    with document:
        if document.needs_pass or not 2 <= len(document) <= 40:
            raise ImportProblem("El PDF debe tener entre 2 y 40 páginas y no estar protegido.")
        answers = extract_key(document[0])
        questions: list[Question] = []
        current: Question | None = None
        option: str | None = None
        for page in list(document)[1:]:
            events = []
            for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    if text:
                        events.append((line["bbox"][1], line["bbox"][0], text))
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
                if re.search(r"Examen teòric|Patrón/ona de Embarcaciones", text):
                    continue
                start = re.match(r"^(\d{1,2})\s*[.)]\s*[-–]?\s*(.*)", text)
                choice = re.match(r"^([a-dA-D])\s*[).]\s*(.*)", text)
                if start:
                    number = int(start[1])
                    if number != len(questions) + 1 or number not in answers:
                        raise ImportProblem(f"Orden de preguntas inesperado: {number}. Revisa el formato.")
                    current = Question(number, start[2], answers[number])
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
        if len(questions) != 45:
            raise ImportProblem(f"Se han encontrado {len(questions)} preguntas; se necesitan 45.")
        for question in questions:
            if not question.text or set(question.options) != set("abcd") or not all(question.options.values()):
                raise ImportProblem(f"La pregunta {question.number} no tiene enunciado y cuatro opciones completas.")
        return questions


def grade(questions: list[dict], answers: dict[str, str], mode: str) -> dict:
    rules = MODES[mode]
    selected = [q for q in questions if q["number"] >= rules["first"]]
    if len(selected) != rules["total"]:
        raise ValueError("El examen no tiene el número de preguntas requerido.")
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
        "failures": failures, "units": list(units.values()), "questions": rows,
    }
