#!/usr/bin/env python3
"""
Приёмник материала для MiroFish.

Движок понимает только pdf/md/txt. Всё остальное приводим к тексту здесь:
  • ссылка на лендинг  → скачиваем страницу и вынимаем читаемый текст;
  • картинка (креатив) → описываем через claude CLI, который умеет смотреть;
  • pdf/md/txt         → отдаём движку как есть.

Зависимостей нет: stdlib + уже установленный claude CLI.
"""

from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("mirofish-ingest")

TEXT_EXT = {".md", ".txt", ".markdown"}
DOC_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED_EXT = TEXT_EXT | DOC_EXT | IMAGE_EXT

UA = "Mozilla/5.0 (compatible; MiroFish/1.0; +https://mozgi.kz)"
FETCH_TIMEOUT = 25
MAX_PAGE_BYTES = 3_000_000
VISION_TIMEOUT = 180

IMAGE_PROMPT = (
    "Опиши этот рекламный материал максимально подробно и буквально, "
    "как будто пересказываешь его человеку, который не видит картинку.\n"
    "Обязательно передай: весь текст дословно (заголовок, подзаголовок, кнопка, "
    "мелкий шрифт, цены, условия), что изображено, кто на изображении и с каким "
    "выражением, общий тон и стиль, какое обещание даётся зрителю и чего в "
    "материале НЕ сказано.\n"
    "Не оценивай и не советуй — только опиши. Ответ на русском."
)


class IngestError(RuntimeError):
    """Материал не удалось привести к тексту."""


TRANSLATE_PROMPT = (
    "Translate this simulation question into clear, concise English. "
    "It describes what a multi-agent audience simulation should predict. "
    "Keep the exact meaning, keep any product or brand names as they are. "
    "Reply with the translation only — no quotes, no preamble.\n\nQuestion: "
)


def translate_requirement(text: str, claude_bin: str = "claude") -> tuple[str, str | None]:
    """
    Промпты движка английские, поэтому русский вопрос переводим.
    Возвращает (требование, заметка) — заметку показываем пользователю.
    """
    text = text.strip()
    if not text or not re.search(r"[а-яёА-ЯЁ]", text):
        return text, None
    try:
        proc = subprocess.run(
            [claude_bin, "-p", TRANSLATE_PROMPT + text, "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("перевод требования не удался: %s", exc)
        return text, "вопрос остался на русском — перевести не удалось"

    translated = (proc.stdout or "").strip().strip('"')
    if proc.returncode != 0 or len(translated) < 10:
        return text, "вопрос остался на русском — перевести не удалось"
    return translated[:600], f"вопрос переведён: «{translated[:120]}»"


# ------------------------------------------------------------------ ссылки

def fetch_url(url: str, dest_dir: Path) -> Path:
    """Скачивает страницу и сохраняет её читаемый текст в .md."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IngestError(f"ссылка должна начинаться с http:// или https:// — получено {url!r}")

    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "ru,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_PAGE_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IngestError(f"страница не открылась: {exc}") from exc

    if "html" not in ctype and "text" not in ctype:
        raise IngestError(f"по ссылке не страница, а {ctype or 'неизвестный тип'}")

    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    if match:
        charset = match.group(1)
    page = raw.decode(charset, errors="replace")

    title, text = extract_text(page)
    if len(text) < 200:
        raise IngestError("на странице почти нет текста — возможно, он подгружается "
                          "скриптами. Скопируй содержимое руками.")

    dest = dest_dir / f"{safe_name(title or parsed.netloc)}.md"
    dest.write_text(f"# {title or url}\n\nИсточник: {url}\n\n{text}\n", encoding="utf-8")
    return dest


def extract_text(page: str) -> tuple[str, str]:
    """Грубое, но предсказуемое извлечение текста: без внешних парсеров."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""

    body = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", page)
    # Блочные теги превращаем в переносы, чтобы абзацы не слиплись в кашу.
    body = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", body)
    body = re.sub(r"(?i)<li[^>]*>", "• ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)

    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in body.splitlines()]
    cleaned, previous = [], None
    for line in lines:
        if not line or line == previous:
            continue
        cleaned.append(line)
        previous = line
    return title, "\n".join(cleaned)


# ---------------------------------------------------------------- картинки

def describe_image(path: Path, dest_dir: Path, claude_bin: str = "claude") -> Path:
    """Просит claude CLI пересказать креатив словами и кладёт результат в .md."""
    prompt = f"{IMAGE_PROMPT}\n\nФайл: {path}"
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=VISION_TIMEOUT, stdin=subprocess.DEVNULL,
            cwd=str(dest_dir), env={**os.environ},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestError(f"не удалось описать картинку: {exc}") from exc

    description = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(description) < 80:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise IngestError(f"картинку описать не вышло: {detail or 'пустой ответ'}")

    dest = dest_dir / f"{path.stem}-описание.md"
    dest.write_text(
        f"# Рекламный креатив: {path.name}\n\n"
        f"_Текстовое описание изображения — движок не умеет смотреть картинки._\n\n"
        f"{description}\n",
        encoding="utf-8",
    )
    return dest


# -------------------------------------------------------------------- общее

def safe_name(raw: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "", raw, flags=re.U).strip().replace(" ", "-")
    return (name[:60] or "источник").lower()


def prepare(dest_dir: Path, *, text: str = "", url: str = "",
            uploads: list[tuple[str, bytes]] | None = None,
            claude_bin: str = "claude") -> tuple[list[Path], list[str]]:
    """
    Приводит всё присланное к списку файлов для движка.
    Возвращает (файлы, заметки) — заметки показываем пользователю.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    notes: list[str] = []

    if text.strip():
        path = dest_dir / "текст.md"
        path.write_text(text.strip(), encoding="utf-8")
        files.append(path)

    for filename, payload in (uploads or []):
        if not payload:
            continue
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXT:
            notes.append(f"«{filename}» пропущен: движок не читает {suffix or 'файлы без расширения'}")
            continue
        saved = dest_dir / f"{safe_name(Path(filename).stem)}{suffix}"
        saved.write_bytes(payload)
        if suffix in IMAGE_EXT:
            try:
                files.append(describe_image(saved, dest_dir, claude_bin))
                notes.append(f"«{filename}» — картинка, пересказана словами")
            except IngestError as exc:
                notes.append(f"«{filename}» пропущен: {exc}")
        else:
            files.append(saved)

    if url.strip():
        try:
            fetched = fetch_url(url.strip(), dest_dir)
            files.append(fetched)
            notes.append(f"страница {url.strip()} скачана")
        except IngestError as exc:
            notes.append(f"ссылка пропущена: {exc}")

    if not files:
        raise IngestError("нечего анализировать: " + ("; ".join(notes) or "пустая форма"))
    return files, notes
