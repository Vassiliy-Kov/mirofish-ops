#!/usr/bin/env python3
"""
MiroFish Telegram bot — запуск симуляций аудитории с телефона.

Приходит текст или файл → задача в очередь → одна симуляция за раз →
по готовности выжимка из verdict.json + report.md файлом.

Зависимостей нет намеренно: только stdlib. Бот крутится long-polling'ом,
наружу ничего не слушает.

Конфиг — /opt/mirofish/.env:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_ALLOWED_USERS=123456789,987654321   # числовые id, не @username
    MIROFISH_MAX_ROUNDS=2
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP_DIR = Path(os.environ.get("MIROFISH_APP_DIR", "/opt/mirofish/app"))
STATE_DIR = Path(os.environ.get("MIROFISH_STATE_DIR", "/opt/mirofish/state"))
INBOX_DIR = STATE_DIR / "inbox"
MIROFISH_BIN = APP_DIR / ".venv" / "bin" / "mirofish"
RUNS_DIR = APP_DIR / "uploads" / "runs"

DEFAULT_REQUIREMENT = (
    "Predict how the target audience will react to this material: "
    "main objections, trust breakers, and which claims fail verification."
)
# Прогон упирается в вызовы claude CLI, 2 раунда на ПК шли ~10 минут.
RUN_TIMEOUT_SEC = int(os.environ.get("MIROFISH_RUN_TIMEOUT_SEC", str(4 * 60 * 60)))
MAX_ROUNDS = os.environ.get("MIROFISH_MAX_ROUNDS", "2")
API = "https://api.telegram.org"

log = logging.getLogger("mirofish-bot")


# ---------------------------------------------------------------- конфиг

def load_env(path: Path = Path("/opt/mirofish/.env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# ------------------------------------------------------------ telegram io

class Telegram:
    def __init__(self, token: str) -> None:
        self._base = f"{API}/bot{token}"
        self._file_base = f"{API}/file/bot{token}"

    def _call(self, method: str, payload: dict, timeout: int = 70) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_updates(self, offset: int) -> list[dict]:
        try:
            result = self._call(
                "getUpdates",
                {"offset": offset, "timeout": 50, "allowed_updates": ["message"]},
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(3)
            return []
        return result.get("result", [])

    def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        # Telegram рубит сообщения длиннее 4096 символов.
        for chunk in _split(text, 3900):
            try:
                self._call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        **({"reply_to_message_id": reply_to} if reply_to else {}),
                    },
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001 — бот не должен падать из-за отправки
                log.warning("sendMessage: %s", exc)

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        try:
            body, content_type = _multipart(
                fields={"chat_id": str(chat_id), "caption": caption[:1000]},
                file_field="document",
                file_path=path,
            )
            req = urllib.request.Request(
                f"{self._base}/sendDocument",
                data=body,
                headers={"Content-Type": content_type},
            )
            urllib.request.urlopen(req, timeout=120).read()
        except Exception as exc:  # noqa: BLE001
            log.warning("sendDocument(%s): %s", path.name, exc)

    def download(self, file_id: str, dest_dir: Path) -> Path | None:
        try:
            info = self._call("getFile", {"file_id": file_id}, timeout=30)
            file_path = info["result"]["file_path"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / Path(file_path).name
            with urllib.request.urlopen(
                f"{self._file_base}/{file_path}", timeout=120
            ) as resp, dest.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
            return dest
        except Exception as exc:  # noqa: BLE001
            log.warning("download(%s): %s", file_id, exc)
            return None


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def _multipart(fields: dict, file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----mirofish{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    out += f"Content-Type: {mime}\r\n\r\n".encode()
    out += file_path.read_bytes()
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------- отчёт

def _as_text(value, depth: int = 0) -> str:
    """Форматирует значение verdict.json, не зная его схемы заранее."""
    pad = "  " * depth
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            rendered = _as_text(item, depth + 1)
            if "\n" in rendered:
                lines.append(f"{pad}• <b>{_esc(str(key))}</b>:\n{rendered}")
            else:
                lines.append(f"{pad}• <b>{_esc(str(key))}</b>: {rendered.strip()}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(f"{pad}– {_as_text(v, depth + 1).strip()}" for v in value[:12])
    text = str(value)
    return pad + _esc(text if len(text) < 900 else text[:900] + "…")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def summarize(run_dir: Path) -> str:
    verdict = run_dir / "report" / "verdict.json"
    if not verdict.exists():
        return "Прогон завершился, но <code>verdict.json</code> не найден."
    try:
        data = json.loads(verdict.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"verdict.json не читается: {_esc(str(exc))}"

    # Схема форка может меняться — тянем известные ключи, остальное показываем как есть.
    preferred = ("verdict", "prediction", "summary", "confidence", "key_dynamics",
                 "dynamics", "signals", "risks", "recommendations")
    head, rest = {}, {}
    for key, value in (data.items() if isinstance(data, dict) else []):
        (head if key.lower() in preferred else rest)[key] = value

    body = _as_text(head or data)
    if rest:
        body += "\n\n<i>Остальные поля — в report.md</i>"
    return body


# ------------------------------------------------------------- исполнение

class Worker(threading.Thread):
    """Одна симуляция за раз: больше машина не тянет рядом с боевым mozgi."""

    def __init__(self, tg: Telegram) -> None:
        super().__init__(daemon=True)
        self.tg = tg
        self.jobs: queue.Queue[dict] = queue.Queue()
        self.current: dict | None = None

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            self.current = job
            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001 — воркер не должен умирать
                log.exception("job failed")
                self.tg.send(job["chat_id"], f"❌ Сбой прогона: {_esc(str(exc))}")
            finally:
                self.current = None
                self.jobs.task_done()

    def _execute(self, job: dict) -> None:
        chat_id, material, requirement = job["chat_id"], job["file"], job["requirement"]
        started = time.time()
        self.tg.send(
            chat_id,
            f"🐟 Запустил прогон.\nМатериал: <code>{_esc(material.name)}</code>\n"
            f"Проверяю: <i>{_esc(requirement)}</i>\nРаундов: {MAX_ROUNDS}. "
            f"Это небыстро — напишу, когда будет готово.",
        )

        before = _known_runs()
        cmd = [
            str(MIROFISH_BIN), "run",
            "--files", str(material),
            "--requirement", requirement,
            "--max-rounds", str(MAX_ROUNDS),
            "--json",
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SEC,
            env={**os.environ, "LLM_PROVIDER": "claude-cli"},
        )
        minutes = (time.time() - started) / 60

        run_dir = _new_run(before)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
            self.tg.send(
                chat_id,
                f"❌ Прогон упал (код {proc.returncode}, {minutes:.0f} мин).\n"
                f"<pre>{_esc(tail)}</pre>",
            )
            return

        if run_dir is None:
            self.tg.send(chat_id, f"⚠️ Прогон отработал за {minutes:.0f} мин, "
                                  "но папку с отчётом я не нашёл.")
            return

        self.tg.send(
            chat_id,
            f"✅ Готово за {minutes:.0f} мин · <code>{_esc(run_dir.name)}</code>\n\n"
            + summarize(run_dir),
        )
        report = run_dir / "report" / "report.md"
        if report.exists():
            self.tg.send_document(chat_id, report, caption=run_dir.name)


def _known_runs() -> set[str]:
    return {p.name for p in RUNS_DIR.glob("*")} if RUNS_DIR.exists() else set()


def _new_run(before: set[str]) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    fresh = [p for p in RUNS_DIR.glob("*") if p.name not in before and p.is_dir()]
    if fresh:
        return max(fresh, key=lambda p: p.stat().st_mtime)
    existing = [p for p in RUNS_DIR.glob("*") if p.is_dir()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


# ---------------------------------------------------------------- разбор

HELP = (
    "🐟 <b>MiroFish</b> — прогон текста на синтетической аудитории.\n\n"
    "Пришли <b>текст</b> или <b>файл</b> (.md/.txt) — верну разбор: "
    "какие возражения всплывут, где рушится доверие, что не проходит проверку.\n\n"
    "Что именно проверить — первой строкой начиная с <code>?</code>, "
    "по-английски (промпты движка английские):\n"
    "<code>?How will Almaty parents react to this price?</code>\n"
    "Материал — дальше или файлом.\n\n"
    "Команды: /status — очередь · /runs — последние прогоны · /help"
)


def parse_material(text: str) -> tuple[str, str]:
    """Возвращает (requirement, материал). Первая строка с '?' — требование."""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("?"):
        return lines[0].strip().lstrip("?").strip() or DEFAULT_REQUIREMENT, \
               "\n".join(lines[1:]).strip()
    return DEFAULT_REQUIREMENT, text.strip()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN не задан (/opt/mirofish/.env)")
        return 1
    allowed = {
        int(x) for x in re.findall(r"\d+", os.environ.get("TELEGRAM_ALLOWED_USERS", ""))
    }
    if not allowed:
        log.error("TELEGRAM_ALLOWED_USERS пуст — бот открыт всем, так нельзя")
        return 1
    if not MIROFISH_BIN.exists():
        log.error("не найден %s", MIROFISH_BIN)
        return 1

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    tg = Telegram(token)
    worker = Worker(tg)
    worker.start()
    log.info("бот запущен, разрешённых пользователей: %d", len(allowed))

    offset = 0
    while True:
        for update in tg.get_updates(offset):
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            chat_id = message["chat"]["id"]
            user_id = message.get("from", {}).get("id")
            if user_id not in allowed:
                log.warning("чужой пользователь %s", user_id)
                continue
            handle(tg, worker, message, chat_id)
    return 0


def handle(tg: Telegram, worker: Worker, message: dict, chat_id: int) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

    if command in ("/start", "/help"):
        tg.send(chat_id, HELP)
        return
    if command == "/status":
        current = worker.current
        busy = (f"сейчас считается <code>{_esc(current['file'].name)}</code>"
                if current else "свободен")
        tg.send(chat_id, f"Очередь: {worker.jobs.qsize()} · {busy}")
        return
    if command == "/runs":
        runs = sorted(
            (p for p in RUNS_DIR.glob("*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:10] if RUNS_DIR.exists() else []
        tg.send(chat_id, "Последние прогоны:\n" + ("\n".join(
            f"• <code>{_esc(p.name)}</code> — "
            f"{time.strftime('%d.%m %H:%M', time.localtime(p.stat().st_mtime))}"
            for p in runs) or "пока ни одного"))
        return

    document = message.get("document")
    if document:
        requirement, _ = parse_material(text)
        path = tg.download(document["file_id"], INBOX_DIR)
        if path is None:
            tg.send(chat_id, "❌ Не смог скачать файл.")
            return
    else:
        requirement, material = parse_material(text)
        if len(material) < 80:
            tg.send(chat_id, "Нужен текст подлиннее — оффер, прайс, лендинг или пост. "
                             "/help покажет формат.")
            return
        path = INBOX_DIR / f"msg-{int(time.time())}-{message['message_id']}.md"
        path.write_text(material, encoding="utf-8")

    worker.jobs.put({"chat_id": chat_id, "file": path, "requirement": requirement})
    if worker.current is not None:
        tg.send(chat_id, f"⏳ Принял, в очереди {worker.jobs.qsize()}-м "
                         "(одна симуляция за раз).")


if __name__ == "__main__":
    sys.exit(main())
