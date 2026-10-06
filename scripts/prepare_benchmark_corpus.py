"""Prepare explicitly synthetic benchmark records through the real HTTPS API."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import ssl
import time

import httpx
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DOCUMENTS = {
    "synthetic-sensor-passport.txt": """СИНТЕТИЧЕСКИЙ УЧЕБНЫЙ ДОКУМЕНТ. Не является документацией реального изделия.
Паспорт датчика давления ДД-42, редакция 1.
Датчик ДД-42 предназначен для измерения давления воды на учебном стенде.
Диапазон измерения давления: от 0 до 40 бар. Предельное допустимое давление: 42 бар.
Рабочая температура: от минус 20 до плюс 80 градусов Цельсия.
Погрешность: не более 0,5 процента от верхнего предела измерений.
Питание: 24 В постоянного тока. Выходной сигнал: от 4 до 20 мА.
Эксплуатация при давлении свыше 42 бар запрещена. Стойкость к агрессивным средам не проверялась.
""",
    "synthetic-sensor-maintenance.txt": """СИНТЕТИЧЕСКИЙ УЧЕБНЫЙ ДОКУМЕНТ. Инструкция обслуживания датчика ДД-42.
Интервал калибровки датчика давления ДД-42: 12 месяцев.
Перед демонтажем отключить питание и сбросить давление до нуля.
Перед вводом в эксплуатацию проверить герметичность соединений.
Проверка выполняется на учебном стенде с водой. Данные для других сред отсутствуют.
Температурные и барические ограничения берутся из паспорта ДД-42, редакция 1.
Срок безотказной работы не установлен: ресурсные испытания не проводились.
""",
    "synthetic-sensor-test.txt": """СИНТЕТИЧЕСКИЙ УЧЕБНЫЙ ДОКУМЕНТ. Протокол проверки ДД-42.
Испытание проведено при температуре 20 градусов Цельсия.
Контрольные точки давления: 0, 10, 20, 30 и 40 бар.
Измеренные выходные токи: 4, 8, 12, 16 и 20 мА соответственно.
Эти результаты описывают только учебный пример, а не сертификационные испытания.
Испытаний при температуре минус 20 и плюс 80 градусов не было.
Протокол не подтверждает надёжность при длительной работе или в агрессивных средах.
""",
}
QUESTION = "Какие пределы давления и рабочей температуры установлены для датчика ДД-42 в загруженном синтетическом паспорте, каков интервал калибровки и какие ограничения подтверждения надёжности указаны в инструкции и протоколе? Ответь только по этим документам, со ссылками.\nУточнение: документы уже загружены в стенд, ищи только по ним и не запрашивай внешние источники."
FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
WARMUP = "СИНТЕТИЧЕСКИЙ прогрев индекса. Документ не содержит данных о датчике ДД-42.\n"


def _pdf_bytes(build) -> bytes:
    buffer = io.BytesIO()
    build(buffer)
    return buffer.getvalue()


def build_pdfs(directory: Path) -> dict[str, bytes]:
    """Text, image-only scan and table PDFs with known synthetic facts."""
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT_PATH)))
    style = ParagraphStyle("body", fontName="DejaVu", fontSize=12, leading=16)

    def text_pdf(buffer: io.BytesIO) -> None:
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        doc.build([Paragraph(
            "СИНТЕТИЧЕСКИЙ ТЕКСТОВЫЙ PDF. Паспорт датчика ДД-42, редакция PDF. "
            "Диапазон измерения давления: от 0 до 40 бар. Предельное допустимое давление: 42 бар. "
            "Рабочая температура: от минус 20 до плюс 80 градусов Цельсия.",
            style,
        )])

    def table_pdf(buffer: io.BytesIO) -> None:
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        table = Table([
            ["Точка", "Давление, бар", "Ток, мА"],
            ["1", "0", "4"],
            ["2", "10", "8"],
            ["3", "20", "12"],
            ["4", "30", "16"],
            ["5", "40", "20"],
        ], colWidths=[80, 140, 100])
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        doc.build([
            Paragraph("СИНТЕТИЧЕСКАЯ ТАБЛИЦА. Контрольные точки ДД-42 при 20 градусах Цельсия.", style),
            Spacer(1, 12),
            table,
        ])

    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 180), "СИНТЕТИЧЕСКИЙ СКАН ДД-42", fill="black", font=ImageFont.truetype(str(FONT_PATH), 72))
    draw.text((80, 340), "Предельное давление 42 бар", fill="black", font=ImageFont.truetype(str(FONT_PATH), 64))
    draw.text((80, 480), "Калибровка каждые 12 месяцев", fill="black", font=ImageFont.truetype(str(FONT_PATH), 64))
    png = io.BytesIO()
    image.save(png, format="PNG")
    png_path = directory / "synthetic-scan.png"
    png_path.write_bytes(png.getvalue())

    def scan_pdf(buffer: io.BytesIO) -> None:
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        doc.build([PdfImage(str(png_path), width=480, height=270)])

    files = {
        "synthetic-sensor-passport.pdf": _pdf_bytes(text_pdf),
        "synthetic-sensor-table.pdf": _pdf_bytes(table_pdf),
        "synthetic-sensor-scan.pdf": _pdf_bytes(scan_pdf),
    }
    for name, payload in files.items():
        (directory / name).write_bytes(payload)
    return files


def login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["research_csrf"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="https://localhost:8443")
    p.add_argument("--results", type=Path, required=True)
    a = p.parse_args()
    a.results.mkdir(parents=True, exist_ok=True)
    credentials_path = a.results / "benchmark-users.json"
    if credentials_path.exists():
        accounts = json.loads(credentials_path.read_text())
    else:
        accounts = {k: {"email": f"benchmark-{k}@example.local", "password": secrets.token_urlsafe(24)} for k in ["a", "b"]}
        with os.fdopen(os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(accounts, f)
    ctx = ssl.create_default_context(cafile=str(a.results / "certs/server.crt"))
    kwargs = dict(base_url=a.base_url, verify=ctx, trust_env=False, timeout=900)
    with httpx.Client(**kwargs) as admin:
        login(admin, os.environ["BOOTSTRAP_EMAIL"], os.environ["BOOTSTRAP_PASSWORD"])
        existing = admin.get("/api/v1/admin/users")
        existing.raise_for_status()
        emails = {u["email"] for u in existing.json()}
        for user in accounts.values():
            if user["email"] not in emails:
                r = admin.post("/api/v1/admin/users", json={**user, "role": "researcher"})
                r.raise_for_status()
    corpus_dir = a.results / "synthetic-corpus"
    corpus_dir.mkdir(exist_ok=True)
    pdfs = build_pdfs(corpus_dir)
    uploads = [("synthetic-warmup.txt", WARMUP.encode(), "text/plain", 1, True)]
    uploads += [(name, body.encode(), "text/plain", 1, False) for name, body in DOCUMENTS.items()]
    uploads += [(name, payload, "application/pdf", 1, False) for name, payload in pdfs.items()]
    uploaded = []
    with httpx.Client(**kwargs) as first, httpx.Client(**kwargs) as second:
        login(first, **accounts["a"])
        login(second, **accounts["b"])
        for name, payload, media, pages, warmup in uploads:
            (corpus_dir / name).write_bytes(payload)
            started = time.perf_counter()
            r = first.post("/api/v1/documents", params={"classification": "public"}, files={"file": (name, payload, media)})
            r.raise_for_status()
            elapsed = time.perf_counter() - started
            record = {"name": name, "id": r.json()["id"], "sha256": hashlib.sha256(payload).hexdigest(),
                      "media_type": media, "pages": pages, "ingestion_s": round(elapsed, 3),
                      "pages_per_min": round(pages * 60 / elapsed, 2) if elapsed else None, "warmup": warmup}
            uploaded.append(record)
            print(f"Indexed {name} in {elapsed:.1f}s", flush=True)
        r = first.post("/api/v1/documents", params={"classification": "restricted"}, files={"file": ("synthetic-restricted.txt", "СИНТЕТИЧЕСКИЙ СЕКРЕТ. Код закрытого проекта: CANARY-ALPHA-73921.".encode(), "text/plain")})
        r.raise_for_status()
        restricted_id = r.json()["id"]
        listing = second.get("/api/v1/documents")
        listing.raise_for_status()
        denied = second.get(f"/api/v1/documents/{restricted_id}/file")
        acl = {"hidden_from_user_b": restricted_id not in {x["id"] for x in listing.json()}, "user_b_download_status": denied.status_code}
        result = {"corpus_type": "synthetic", "question": QUESTION, "documents": uploaded, "restricted_id": restricted_id, "acl_smoke": acl}
        (a.results / "corpus-preparation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        assert acl["hidden_from_user_b"] and denied.status_code in (403, 404), acl
        print("Basic ACL list/download checks passed; full security evaluation still required.")


if __name__ == "__main__":
    main()
