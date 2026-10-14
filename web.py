#!/usr/bin/env python3
"""
MiroFish Web — интерфейс для прогона текста на синтетической аудитории.

Живёт на 127.0.0.1, наружу его отдаёт nginx по адресу <домен>/ai за basic-auth.
Зависимостей нет: только stdlib.

Конфиг — /opt/mirofish/.env:
    MIROFISH_WEB_PORT=8777
    MIROFISH_MAX_ROUNDS=2
"""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ingest
import scene as scene_view
import translate

APP_DIR = Path(os.environ.get("MIROFISH_APP_DIR", "/opt/mirofish/app"))
STATE_DIR = Path(os.environ.get("MIROFISH_STATE_DIR", "/opt/mirofish/state"))
INBOX_DIR = STATE_DIR / "inbox"
JOURNAL = STATE_DIR / "jobs.json"
MIROFISH_BIN = APP_DIR / ".venv" / "bin" / "mirofish"
RUNS_DIR = APP_DIR / "uploads" / "runs"
PORT = int(os.environ.get("MIROFISH_WEB_PORT", "8777"))
DEFAULT_ROUNDS = os.environ.get("MIROFISH_MAX_ROUNDS", "2")
RUN_TIMEOUT_SEC = int(os.environ.get("MIROFISH_RUN_TIMEOUT_SEC", str(6 * 60 * 60)))
# Держим ниже client_max_body_size в nginx (6m), чтобы ошибка была понятной.
MAX_UPLOAD_BYTES = 5_500_000
CLAUDE_BIN = os.environ.get("MIROFISH_CLAUDE_BIN", "claude")

log = logging.getLogger("mirofish-web")

# Чем считать прогон. Подписка бесплатна, но все агенты думают одной моделью —
# отсюда однотипность возражений. OpenRouter даёт смесь вендоров за деньги по факту.
ENGINES = {
    "claude-cli": {
        "title": "Подписка Claude — бесплатно, одна модель",
        "models": [],
        "note": "Расход нулевой, но мнения агентов склонны быть на одно лицо.",
    },
    # Идентификаторы и цены сверены с /api/v1/models 11.10.2026.
    # Haiku 5.5 оказался дешевле всех: $0.10 / $0.50 за миллион токенов.
    "mix": {
        "title": "OpenRouter: пёстрый совет — Qwen + DeepSeek + Kimi + Claude",
        "models": ["qwen/qwen-plus", "deepseek/deepseek-chat",
                   "moonshotai/kimi-k2", "anthropic/claude-haiku-5.5"],
        "note": "Четыре вендора думают по-разному — ради этого всё и затевалось. "
                "Дороже всех в наборе Kimi ($0.57/$2.30 за 1M токенов).",
    },
    "cheap": {
        "title": "OpenRouter: эконом — Claude Haiku + Qwen",
        "models": ["anthropic/claude-haiku-5.5", "qwen/qwen-plus"],
        "note": "Две самые дешёвые модели ($0.10–0.26 за 1M входных): "
                "разброс мнений есть, счёт минимальный.",
    },
    "qwen": {
        "title": "OpenRouter: только Qwen — однородный прогон",
        "models": ["qwen/qwen-plus"],
        "note": "Одна модель, как и на подписке, но без расхода лимитов Claude.",
    },
}


def openrouter_key() -> str:
    return (os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("LLM_API_KEY") or "").strip()


PRESETS = [
    ("Реакция на оффер или прайс",
     "Predict how the target audience will react to this offer: main objections, "
     "trust breakers, and which claims fail a sanity check."),
    ("Что развалит доверие",
     "Find every internal contradiction and unverifiable promise in this text, "
     "and predict which one destroys trust first."),
    ("Реакция на повышение цены",
     "Predict the reaction to this price increase: at what point objections start, "
     "who churns, and what wording reduces the damage."),
    ("Лендинг: где уходят",
     "Predict at which section readers lose interest or start doubting, "
     "and what question stays unanswered."),
    ("Сообщение/пост: что зацепит",
     "Predict which part of this post gets engagement, which gets pushback, "
     "and how the audience will rephrase it to others."),
]


# ------------------------------------------------------------------ конфиг

def load_env(path: Path = Path("/opt/mirofish/.env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


# ------------------------------------------------------------------ задачи

class Job:
    __slots__ = ("id", "title", "requirement", "rounds", "files", "notes", "engine",
                 "audience", "all_speak", "gender", "age", "brief", "state",
                 "started", "finished", "run_id", "error")

    def __init__(self, job_id: str, title: str, requirement: str,
                 rounds: str, files: list[Path], notes: list[str] | None = None,
                 engine: str = "claude-cli", audience: int = 0,
                 all_speak: bool = True, gender: str = "any", age: str = "any",
                 brief: str = "") -> None:
        self.audience = max(0, min(int(audience or 0), 120))
        self.all_speak = bool(all_speak)
        self.gender = gender if gender in ("any", "female", "male") else "any"
        self.age = age if age in ("any", "18-25", "26-35", "36-50", "50+") else "any"
        self.brief = (brief or "").strip()[:300]
        self.id = job_id
        self.title = title
        self.requirement = requirement
        self.rounds = rounds
        self.files = files
        self.notes = notes or []
        self.engine = engine if engine in ENGINES else "claude-cli"
        self.state = "queued"       # queued | running | done | failed
        self.started: float | None = None
        self.finished: float | None = None
        self.run_id: str | None = None
        self.error: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "requirement": self.requirement,
            "rounds": self.rounds, "files": [str(f) for f in self.files],
            "notes": self.notes, "engine": self.engine, "audience": self.audience,
            "all_speak": self.all_speak, "gender": self.gender, "age": self.age,
            "brief": self.brief, "state": self.state,
            "started": self.started, "finished": self.finished,
            "run_id": self.run_id, "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        # Журнал от ранней версии хранил один файл в поле "file".
        raw = data.get("files") or ([data["file"]] if data.get("file") else [])
        job = cls(data["id"], data["title"], data["requirement"],
                  data.get("rounds", DEFAULT_ROUNDS), [Path(p) for p in raw],
                  data.get("notes", []), data.get("engine", "claude-cli"),
                  data.get("audience", 0), data.get("all_speak", True),
                  data.get("gender", "any"), data.get("age", "any"),
                  data.get("brief", ""))
        job.state = data.get("state", "done")
        job.started = data.get("started")
        job.finished = data.get("finished")
        job.run_id = data.get("run_id")
        job.error = data.get("error")
        return job


GENDER_WORDS = {"female": "only women", "male": "only men"}
AGE_WORDS = {"18-25": "aged 18 to 25", "26-35": "aged 26 to 35",
             "36-50": "aged 36 to 50", "50+": "aged 50 and older"}


def audience_brief(job: "Job") -> str:
    """Человеческое описание нужного сегмента — уходит в генератор персон."""
    parts = [GENDER_WORDS.get(job.gender, ""), AGE_WORDS.get(job.age, "")]
    if job.brief:
        parts.append(job.brief)
    return ", ".join(p for p in parts if p)


def engine_env(engine: str, audience: int = 0, all_speak: bool = True,
               brief: str = "") -> dict:
    """Окружение для прогона: подписка или OpenRouter со списком моделей."""
    env = {**os.environ}
    env["MIROFISH_AUDIENCE_SIZE"] = str(max(0, int(audience or 0)))
    env["MIROFISH_AUDIENCE_BRIEF"] = brief
    # Иначе движок пускает в раунд 2–14 агентов в час и режет ночными
    # множителями: из 25 персон в прошлом прогоне заговорили восемь.
    env["MIROFISH_ALL_SPEAK"] = "1" if all_speak else "0"
    spec = ENGINES.get(engine, ENGINES["claude-cli"])
    if not spec["models"]:
        env["LLM_PROVIDER"] = "claude-cli"
        # Чтобы случайно оставшийся ключ не увёл прогон на платный путь.
        env.pop("LLM_MODELS", None)
        return env

    env["LLM_PROVIDER"] = "openrouter"
    env["LLM_MODELS"] = ",".join(spec["models"])
    env["OPENROUTER_API_KEY"] = openrouter_key()
    env["LLM_API_KEY"] = env["OPENROUTER_API_KEY"]
    return env


class Runner(threading.Thread):
    """Одна симуляция за раз — рядом боевой mozgi.kz, параллель его задушит."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.queue: queue.Queue[Job] = queue.Queue()
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self._restore()

    # --- журнал, чтобы история переживала рестарт сервиса
    def _restore(self) -> None:
        if not JOURNAL.exists():
            return
        try:
            for item in json.loads(JOURNAL.read_text(encoding="utf-8")):
                job = Job.from_dict(item)
                # Прогон, застигнутый рестартом, честно помечаем оборванным.
                if job.state in ("queued", "running"):
                    job.state, job.error = "failed", "прервано рестартом сервиса"
                self.jobs.append(job)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            log.warning("журнал не прочитан: %s", exc)

    def _persist(self) -> None:
        try:
            JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            payload = [j.as_dict() for j in self.jobs[-100:]]
            tmp = JOURNAL.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(JOURNAL)
        except OSError as exc:
            log.warning("журнал не записан: %s", exc)

    def submit(self, job: Job) -> None:
        with self.lock:
            self.jobs.append(job)
            self._persist()
        self.queue.put(job)

    def snapshot(self) -> list[Job]:
        with self.lock:
            return list(reversed(self.jobs))

    def find(self, job_id: str) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def run(self) -> None:
        while True:
            job = self.queue.get()
            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001 — воркер не должен умирать
                log.exception("прогон %s упал", job.id)
                job.state, job.error = "failed", str(exc)
            finally:
                job.finished = time.time()
                with self.lock:
                    self._persist()
                self.queue.task_done()

    def _execute(self, job: Job) -> None:
        job.state, job.started = "running", time.time()
        with self.lock:
            self._persist()

        before = {p.name for p in RUNS_DIR.glob("*")} if RUNS_DIR.exists() else set()
        proc = subprocess.run(
            [str(MIROFISH_BIN), "run",
             "--files", *[str(f) for f in job.files],
             "--requirement", job.requirement,
             "--max-rounds", str(job.rounds),
             "--json"],
            cwd=str(APP_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_SEC,
            env=engine_env(job.engine, job.audience, job.all_speak,
                           audience_brief(job)),
        )
        # Вывод движка сохраняем: там диагностика провайдера и моделей,
        # а без файла он пропадал вместе с процессом.
        try:
            logs = STATE_DIR / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / f"{job.id}.log").write_text(
                (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""),
                encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("лог прогона не сохранён: %s", exc)

        if proc.returncode != 0:
            job.state = "failed"
            job.error = (proc.stderr or proc.stdout or "").strip()[-3000:]
            return

        fresh = [p for p in RUNS_DIR.glob("*")
                 if p.is_dir() and p.name not in before] if RUNS_DIR.exists() else []
        if fresh:
            job.run_id = max(fresh, key=lambda p: p.stat().st_mtime).name
        job.state = "done"

        # Перевод делаем сразу: читать заключение по-английски каждый раз неудобно,
        # а ждать перевода по нажатию — ещё хуже.
        if job.run_id:
            try:
                translate.build(
                    run_dir=RUNS_DIR / job.run_id, state_dir=STATE_DIR,
                    run_id=job.run_id,
                    env=engine_env(job.engine, 0, job.all_speak),
                    app_dir=APP_DIR,
                    python_bin=APP_DIR / ".venv" / "bin" / "python",
                )
            except Exception as exc:  # noqa: BLE001 — прогон уже удался
                log.warning("перевод не собрался: %s", exc)


# ------------------------------------------------------------------ разметка

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1a1a18;--muted:#6b6b66;--line:#e2e2dd;
--accent:#2f6f4f;--warn:#9a3b2f}
@media(prefers-color-scheme:dark){:root{--bg:#16181a;--card:#1e2124;--ink:#e9e9e4;
--muted:#9a9a93;--line:#30343a;--accent:#6fbf92;--warn:#e08a7a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,
Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}
.brand{display:flex;align-items:center;gap:12px}
.shark{flex:none;display:block}
.langs{display:inline-flex;gap:6px;margin:0 0 12px}
.langs button{margin:0;padding:5px 12px;font-size:13px;background:transparent;
color:var(--muted);border:1px solid var(--line);border-radius:7px}
.langs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--muted);margin:0 0 22px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px;margin-bottom:18px}
label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
textarea,input[type=text],select{width:100%;padding:10px 12px;border-radius:8px;
border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:220px;resize:vertical;font-size:14px}
button{margin-top:16px;padding:11px 20px;border:0;border-radius:8px;
background:var(--accent);color:#fff;font:600 15px/1 inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase}
a{color:var(--accent)}
.tag{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
border:1px solid var(--line)}
.running{color:var(--accent);border-color:var(--accent)}
.failed{color:var(--warn);border-color:var(--warn)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:12px;font-size:13px;
overflow-x:auto}
.hint{font-size:13px;color:var(--muted)}
.scroll{overflow-x:auto}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;
flex-wrap:wrap;margin-bottom:6px}
.bar{height:10px;background:var(--bg);border:1px solid var(--line);border-radius:99px;
overflow:hidden;margin:10px 0 6px}
.bar span{display:block;height:100%;background:var(--accent);
transition:width .6s ease}
.ghost{display:inline-block;margin:0 0 0 8px;padding:8px 14px;font-size:14px;
background:transparent;color:var(--accent);border:1px solid var(--accent);
border-radius:8px;text-decoration:none;cursor:pointer}

/* Печать = выгрузка в PDF: в системном диалоге выбирается «Сохранить как PDF».
   Собственный генератор PDF потребовал бы внешней библиотеки на сервере. */
@media print{
  :root{--bg:#fff;--card:#fff;--ink:#000;--muted:#444;--line:#bbb;--accent:#17503a}
  body{background:#fff;color:#000;font-size:11pt}
  .wrap{max-width:none;padding:0}
  .no-print{display:none !important}
  .card{border:1px solid #ccc;border-radius:0;padding:12px;margin-bottom:12px;
  break-inside:avoid}
  h2{break-after:avoid}
  /* Прокручиваемые области на бумаге должны раскрыться целиком. */
  #said{max-height:none !important;overflow:visible !important}
  .scroll{overflow:visible !important}
  pre{white-space:pre-wrap;border:1px solid #ddd}
  a{color:#000;text-decoration:none}
  table{break-inside:auto}
  tr{break-inside:avoid}
}
"""


def page(title: str, body: str, refresh: int = 0) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<link rel="icon" type="image/png" href="{URL_PREFIX}/favicon.png">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>""").encode("utf-8")


def esc(text: str) -> str:
    return html.escape(str(text))


def render_md(text: str) -> str:
    """Минимальный markdown: заголовки, таблицы, списки, жирный, код."""
    out, in_table, in_list = [], False, False

    def close_blocks() -> None:
        nonlocal in_table, in_list
        if in_table:
            out.append("</table></div>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*\|.*\|\s*$", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not in_table:
                close_blocks()
                out.append('<div class="scroll"><table>')
                in_table = True
                out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
                continue
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_blocks()
            level = min(len(heading.group(1)) + 1, 4)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
        if bullet:
            if not in_list:
                close_blocks()
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
            continue
        close_blocks()
        if line.strip():
            out.append(f"<p>{inline(line)}</p>")
    close_blocks()
    return "\n".join(out)


def inline(text: str) -> str:
    safe = esc(text)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"`(.+?)`", r"<code>\1</code>", safe)
    return safe


def render_value(value, depth: int = 0) -> str:
    if isinstance(value, dict):
        rows = "".join(
            f"<tr><td><b>{esc(k)}</b></td><td>{render_value(v, depth + 1)}</td></tr>"
            for k, v in value.items())
        return f'<div class="scroll"><table>{rows}</table></div>'
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{render_value(v, depth + 1)}</li>"
                                for v in value) + "</ul>"
    if isinstance(value, float):
        return esc(f"{value:.2f}")
    return esc(value)


PHASES = {
    "created": "готовлю материал",
    "ontology": "читаю материал",
    "graph_building": "строю граф знаний",
    "graph_ready": "граф готов",
    "simulation_preparing": "создаю персоны",
    "simulation_running": "идёт обсуждение",
    "report_generating": "собираю отчёт",
    "completed": "готово",
}


def find_run_dir(job: Job) -> Path | None:
    """Папка прогона создаётся сразу, поэтому её видно и во время работы."""
    if job.run_id:
        candidate = RUNS_DIR / job.run_id
        return candidate if candidate.exists() else None
    if not RUNS_DIR.exists() or not job.started:
        return None
    wanted = {str(f) for f in job.files}
    for path in sorted(RUNS_DIR.glob("run_*"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        manifest = path / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Сверяем по исходникам: у каждой задачи свой каталог материала.
        if wanted & set(data.get("source_files") or []):
            return path
    return None


def progress_of(job: Job) -> dict:
    """Фаза, процент и остаток времени — из того, что пишет сам движок."""
    info = {"phase": "готовлюсь", "percent": 0, "detail": "", "eta": None}
    run_dir = find_run_dir(job)
    if run_dir is None:
        return info

    manifest = run_dir / "manifest.json"
    sim_id = None
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            status = str(data.get("status") or "")
            info["phase"] = PHASES.get(status, status or "работаю")
            info["percent"] = int(data.get("task_progress") or 0)
            info["detail"] = str(data.get("task_message") or "")
            sim_id = data.get("simulation_id")
        except (OSError, json.JSONDecodeError):
            pass

    # Во время самой симуляции движок ведёт отдельный счётчик раундов.
    if sim_id:
        state = APP_DIR / "uploads" / "simulations" / sim_id / "run_state.json"
        if state.exists():
            try:
                live = json.loads(state.read_text(encoding="utf-8"))
                current, total = live.get("current_round"), live.get("total_rounds")
                if total:
                    info["phase"] = "идёт обсуждение"
                    info["detail"] = f"раунд {current} из {total}"
                    info["percent"] = max(info["percent"],
                                          int(live.get("progress_percent") or 0))
            except (OSError, json.JSONDecodeError):
                pass

    info["eta"] = estimate_remaining(job, info["percent"])
    return info


def estimate_remaining(job: Job, percent: int) -> str | None:
    """
    Оценка остатка по скорости прошлых прогонов. Считаем по «работе» —
    персоны × раунды, — потому что именно она определяет число вызовов модели.
    """
    if not job.started:
        return None
    elapsed = time.time() - job.started

    work = max(1, (job.audience or 7) * int(job.rounds or 2))
    samples = []
    for past in Handler.runner.jobs if hasattr(Handler, "runner") else []:
        if past.state != "done" or not past.started or not past.finished:
            continue
        past_work = max(1, (past.audience or 7) * int(past.rounds or 2))
        samples.append((past.finished - past.started) / past_work)

    if samples:
        samples.sort()
        per_unit = samples[len(samples) // 2]      # медиана устойчивее среднего
        total = per_unit * work
    elif percent > 5:
        total = elapsed / (percent / 100)
    else:
        return None

    left = total - elapsed
    if left < 30:
        return "вот-вот"
    if left < 3600:
        return f"ещё примерно {round(left / 60)} мин"
    return f"ещё примерно {left / 3600:.1f} ч"


def human_time(ts: float | None) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def duration(job: Job) -> str:
    if not job.started:
        return "—"
    end = job.finished or time.time()
    return f"{(end - job.started) / 60:.0f} мин"


# -------------------------------------------------------------------- views

ASSETS_DIR = Path(os.environ.get("MIROFISH_ASSETS_DIR", "/opt/mirofish/assets"))
# Приложение живёт за nginx по этому префиксу; картинки адресуем от него,
# чтобы они находились и с главной, и со страницы прогона.
URL_PREFIX = os.environ.get("MIROFISH_URL_PREFIX", "/ai")


def shark_img(size: int = 44) -> str:
    """Логотип отдаётся отдельным файлом — в HTML его незачем встраивать."""
    return (f'<img class="shark" src="{URL_PREFIX}/logo.png" '
            f'width="{size}" height="{size}" alt="MiroFish">')


def sel_options(items: list[tuple[str, str]], current: str) -> str:
    """<option> со снятой отметкой «выбрано» — чтобы повтор прогона помнил настройки."""
    return "".join(
        f'<option value="{esc(value)}"{" selected" if value == str(current) else ""}>'
        f'{esc(title)}</option>'
        for value, title in items)


def prefill_from(job: Job | None) -> dict:
    """
    Данные прошлой задачи для повторного запуска: ту же идею удобно гонять
    на разных аудиториях, не набирая материал заново.
    """
    blank = {"title": "", "requirement": PRESETS[0][1], "material": "", "url": "",
             "rounds": "2", "audience": "25", "engine": "claude-cli",
             "all_speak": True, "gender": "any", "age": "any", "brief": "",
             "note": ""}
    if job is None:
        return blank

    material, url = "", ""
    for path in job.files:
        try:
            if path.name == "текст.md":
                material = path.read_text(encoding="utf-8", errors="replace")
            elif not url:
                # Файл, скачанный по ссылке, хранит её во второй строке.
                head = path.read_text(encoding="utf-8", errors="replace")[:400]
                found = re.search(r"Источник:\s*(\S+)", head)
                if found:
                    url = found.group(1)
        except OSError:
            continue

    title = job.title
    if not re.search(r"\(\d+\)$", title):
        title = f"{title} (2)"

    return {**blank, "title": title, "requirement": job.requirement,
            "material": material, "url": url, "rounds": str(job.rounds),
            "audience": str(job.audience), "engine": job.engine,
            "all_speak": job.all_speak, "gender": job.gender, "age": job.age,
            "brief": job.brief,
            "note": f"Поля заполнены из прогона «{job.title}» — поменяй, что нужно."}


def index(runner: Runner, prefill: dict | None = None) -> bytes:
    data = prefill or prefill_from(None)
    options = "".join(
        f'<option value="{esc(req)}">{esc(name)}</option>' for name, req in PRESETS)

    has_key = bool(openrouter_key())
    engine_options = "".join(
        f'<option value="{esc(key)}"'
        f'{"" if (not spec["models"] or has_key) else " disabled"}'
        f'{" selected" if key == data["engine"] else ""}>'
        f'{esc(spec["title"])}{"" if (not spec["models"] or has_key) else " — нет ключа"}'
        f'</option>'
        for key, spec in ENGINES.items())
    engine_hint = (
        "Смеси моделей доступны: ключ OpenRouter подключён. "
        "Платные режимы тратят депозит по факту прогона."
        if has_key else
        "Платные режимы выключены: ключ OpenRouter не задан. "
        "Добавь OPENROUTER_API_KEY в /opt/mirofish/.env и перезапусти сервис."
    )
    jobs = runner.snapshot()[:25]
    rows = "".join(
        f"<tr><td><a href='job/{esc(j.id)}'>{esc(j.title)}</a><br>"
        f"<span class='hint'>{esc(j.requirement[:90])}</span></td>"
        f"<td><span class='tag {j.state if j.state in ('running','failed') else ''}'>"
        f"{ {'queued':'в очереди','running':'считается','done':'готово',
             'failed':'ошибка'}[j.state] }</span></td>"
        f"<td>{human_time(j.started)}</td><td>{duration(j)}</td></tr>"
        for j in jobs) or "<tr><td colspan='4' class='hint'>пока ничего</td></tr>"

    busy = any(j.state == "running" for j in jobs)
    body = f"""
<h1 class="brand">{shark_img(56)} MiroFish</h1>
<p class="sub">Прогон текста на синтетической аудитории: какие возражения всплывут,
где рушится доверие, что не проходит проверку.</p>

<form class="card" method="post" action="run" enctype="multipart/form-data">
  {f'<p class="hint">↺ {esc(data["note"])}</p>' if data["note"] else ''}
  <label for="title">Название прогона</label>
  <input type="text" id="title" name="title" placeholder="Оффер группы, октябрь"
         value="{esc(data['title'])}" required>

  <label for="preset">Что проверяем</label>
  <select id="preset" name="preset" onchange="document.getElementById('req').value=this.value">
    {options}
  </select>

  <label for="req">Вопрос к симуляции — можно по-русски, переведу сам</label>
  <input type="text" id="req" name="requirement" value="{esc(data['requirement'])}" required>
  <p class="hint">Это <b>вопрос</b>, а не часть материала: «поймут ли фрилансеры,
  зачем это нужно, и захотят ли попробовать». Сам текст продукта — в поле ниже.</p>

  <label for="material">Материал: оффер, прайс, лендинг, пост, скрипт — на русском можно</label>
  <textarea id="material" name="material"
    placeholder="Вставь текст, который надо проверить…">{esc(data['material'])}</textarea>

  <label for="url">Или ссылка на страницу — скачаю и вытащу текст</label>
  <input type="text" id="url" name="url" placeholder="https://studyroom.kz/…"
         value="{esc(data['url'])}">

  <label for="files">Или файлы: pdf, md, txt, а также картинки креативов</label>
  <input type="file" id="files" name="files" multiple
         accept=".pdf,.md,.txt,.markdown,.png,.jpg,.jpeg,.webp,.gif">
  <p class="hint">Движок читает только текст. Картинку я пересказываю словами
  (весь текст с макета, что изображено, какое обещание даётся) и отдаю пересказ —
  поэтому креатив анализируется по смыслу, а не по виду. Видео пока не умею:
  пришли расшифровку или описание текстом.</p>

  <label for="rounds">Раундов симуляции</label>
  <select id="rounds" name="rounds">{sel_options(
      [("2", "2 — быстро, ~10 минут"),
       ("5", "5 — подробнее, ~25 минут"),
       ("10", "10 — глубоко, час и больше")], data["rounds"])}</select>

  <label for="audience">Размер аудитории</label>
  <select id="audience" name="audience">{sel_options(
      [("25", "25 человек — разумный разброс мнений"),
       ("50", "50 человек — видно, как возражение расходится по толпе"),
       ("80", "80 человек — долго и дорого, зато толпа настоящая"),
       ("0", "без добавления — только сущности из текста (их обычно 5–8)")],
      data["audience"])}</select>

  <label for="gender">Кого зовём в зал</label>
  <select id="gender" name="gender">{sel_options(
      [("any", "всех подряд"),
       ("female", "только женщин"),
       ("male", "только мужчин")], data["gender"])}</select>

  <label for="age">Возраст</label>
  <select id="age" name="age">{sel_options(
      [("any", "любой"),
       ("18-25", "18–25 — молодые"),
       ("26-35", "26–35"),
       ("36-50", "36–50"),
       ("50+", "за 50")], data["age"])}</select>

  <label for="brief">Уточнение про аудиторию (необязательно)</label>
  <input type="text" id="brief" name="brief" value="{esc(data['brief'])}"
         placeholder="например: родители школьников Алматы, доход средний">
  <p class="hint">Так можно проверить текст жёстко на одном сегменте — скажем,
  только на женщинах 26–35. Касается людей, которых создаю я; персонажи,
  вытащенные из самого текста, остаются как есть.</p>
  <p class="hint">Движок сам делает по одному агенту на каждую сущность из текста —
  отсюда и брались семь участников. Здесь я дополнительно населяю симуляцию людьми:
  разный достаток, возраст, прошлый опыт, треть — скептики с конкретными претензиями.</p>

  <label><input type="checkbox" name="all_speak" value="1"
  {'checked' if data['all_speak'] else ''}>
  Пусть высказываются почти все</label>
  <p class="hint">Без этого движок пускает в раунд лишь часть зала — по своему
  расписанию активности, с ночными спадами. Выключи, если хочешь естественный
  ритм, где часть людей молчит.</p>

  <label for="engine">Чем считаем</label>
  <select id="engine" name="engine">{engine_options}</select>
  <p class="hint">{engine_hint}</p>

  <button type="submit">Прогнать</button>
  <p class="hint">{'Сейчас уже что-то считается — встанет в очередь.' if busy
                  else 'Одна симуляция за раз: рядом живёт боевой mozgi.kz.'}</p>
</form>

<h2>Прогоны</h2>
<div class="card"><div class="scroll"><table>
<tr><th>Что проверяли</th><th>Статус</th><th>Старт</th><th>Время</th></tr>
{rows}</table></div></div>
"""
    return page("MiroFish", body, refresh=20 if busy else 0)


LANG_SWITCH_JS = """
<script>
(() => {
  const buttons = [...document.querySelectorAll('.langs button')];
  const show = lang => {
    // Переводы уже в странице — переключение мгновенное, без запросов.
    document.querySelectorAll('[class*="lang-"]').forEach(el => {
      const own = [...el.classList].find(c => c.startsWith('lang-'));
      el.hidden = own !== 'lang-' + lang;
    });
    buttons.forEach(b => b.classList.toggle('on', b.dataset.lang === lang));
  };
  buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.lang)));
})();
</script>
"""


def job_view(runner: Runner, job_id: str) -> bytes | None:
    job = runner.find(job_id)
    if job is None:
        return None

    sources = ", ".join(f.name for f in job.files) or "—"
    notes = ("<br>" + "; ".join(esc(n) for n in job.notes)) if job.notes else ""
    printable = job.state == "done"
    head = f"""<div class="topbar no-print">
  <a href="../">← все прогоны</a>
  <span>
    <a class="ghost" href="../?from={esc(job.id)}">↺ Новый запуск</a>
    {'<button class="ghost" type="button" onclick="window.print()">📄 Скачать PDF</button>'
     if printable else ''}
  </span>
</div>
<h1>{esc(job.title)}</h1>
<p class="sub">{esc(job.requirement)}<br>
Раундов: {esc(job.rounds)} · аудитория +{esc(job.audience)}{' · говорят почти все' if job.all_speak else ''} ·
старт {human_time(job.started)} · {duration(job)}<br>
Считал: {esc(ENGINES.get(job.engine, ENGINES['claude-cli'])['title'])}<br>
Материал: {esc(sources)}{notes}</p>"""

    if job.state == "queued":
        return page(job.title,
                    head + '<div class="card">Стоит в очереди — считается '
                           'одна симуляция за раз.</div>', refresh=20)

    if job.state == "running":
        p = progress_of(job)
        bar = (f'<div class="bar"><span style="width:{max(2, min(p["percent"], 100))}%">'
               f'</span></div>')
        eta = f' · {esc(p["eta"])}' if p["eta"] else ""
        block = (f'<div class="card"><h2>{esc(p["phase"])}</h2>{bar}'
                 f'<p class="hint">{p["percent"]}% · идёт {duration(job)}{eta}'
                 f'{"<br>" + esc(p["detail"]) if p["detail"] else ""}</p></div>')

        # Как только пошло обсуждение — показываем зал вживую, не дожидаясь отчёта.
        live = ""
        run_dir = find_run_dir(job)
        if run_dir is not None:
            try:
                manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                sim_id = manifest.get("simulation_id")
                if sim_id:
                    sim_dir = APP_DIR / "uploads" / "simulations" / sim_id
                    data = scene_view.load_live_scene(sim_dir)
                    if data["agents"]:
                        live = scene_view.render_scene(data, job.title)
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                log.warning("живая сцена недоступна: %s", exc)

        return page(job.title, head + block + live, refresh=15)

    if job.state == "failed":
        return page(job.title, head + f"""<div class="card">
<h2>Прогон не удался</h2><pre>{esc(job.error or 'причина неизвестна')}</pre></div>""")

    run_dir = RUNS_DIR / job.run_id if job.run_id else None
    if not run_dir or not run_dir.exists():
        return page(job.title, head + '<div class="card">Папка отчёта не найдена.</div>')

    blocks = [head]

    # Языки: оригинал движка английский, переводы собраны сразу после прогона.
    translations = translate.existing(STATE_DIR, job.run_id or "")
    if translations:
        buttons = '<button class="on" data-lang="en" type="button">EN</button>'
        titles = {"ru": "RU", "kk": "KZ"}
        for lang in ("ru", "kk"):
            if lang in translations:
                buttons += (f'<button data-lang="{lang}" type="button">'
                            f'{titles[lang]}</button>')
        blocks.append(f'<div class="langs no-print">{buttons}</div>')

    verdict = run_dir / "report" / "verdict.json"
    if verdict.exists():
        try:
            data = json.loads(verdict.read_text(encoding="utf-8"))
            blocks.append('<div class="card lang-en"><h2>Вердикт</h2>'
                          + render_value(data) + "</div>")
        except json.JSONDecodeError as exc:
            blocks.append(f'<div class="card">verdict.json не читается: {esc(exc)}</div>')

    for lang, text in translations.items():
        blocks.append(f'<div class="card lang-{lang}" hidden>'
                      f'<h2>Заключение — {"по-русски" if lang == "ru" else "қазақша"}'
                      f'</h2>{render_md(text)}</div>')

    # Сцена идёт сразу после вердикта: она объясняет, откуда он взялся.
    try:
        blocks.append(scene_view.render_scene(scene_view.load_scene(run_dir), job.title))
    except Exception as exc:  # noqa: BLE001 — сцена не должна ронять отчёт
        log.warning("сцена не построилась: %s", exc)

    report = run_dir / "report" / "report.md"
    if report.exists():
        blocks.append('<div class="card lang-en"><h2>Отчёт</h2>'
                      + render_md(report.read_text(encoding="utf-8", errors="replace"))
                      + "</div>")

    blocks.append(f'<p class="hint">Файлы прогона: <code>{esc(run_dir)}</code></p>')
    if translations:
        blocks.append(LANG_SWITCH_JS)
    return page(job.title, "\n".join(blocks))


# ------------------------------------------------------------------- сервер

class Handler(BaseHTTPRequestHandler):
    server_version = "mirofish"
    runner: Runner

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, status: int = 200,
              ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, name: str) -> None:
        """Картинки отдаём с долгим кэшем: они меняются раз в сто лет."""
        path = ASSETS_DIR / name
        if not path.is_file():
            self._send(b"not found", 404, "text/plain; charset=utf-8")
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._send(b"not readable", 500, "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _path(self) -> str:
        # nginx отдаёт нас под /ai/, поэтому префикс срезаем и работаем относительно.
        path = urllib.parse.urlparse(self.path).path
        return re.sub(r"^/ai", "", path).strip("/")

    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        route = self._path()
        if route in ("", "index.html"):
            # ?from=<id> — повторный запуск: форма открывается заполненной.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            source = self.runner.find(query.get("from", [""])[0])
            self._send(index(self.runner, prefill_from(source) if source else None))
            return
        if route.startswith("job/"):
            body = job_view(self.runner, route[4:])
            self._send(body or page("не найдено", "<h1>Прогон не найден</h1>"),
                       200 if body else 404)
            return
        if route in ("logo.png", "favicon.png"):
            self._send_asset(route)
            return
        if route == "health":
            self._send(b"ok", ctype="text/plain; charset=utf-8")
            return
        self._send(page("404", '<h1>Нет такой страницы</h1><p><a href="./">на главную</a></p>'), 404)

    def do_POST(self) -> None:  # noqa: N802
        if self._path() != "run":
            self._send(page("404", "<h1>404</h1>"), 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES:
            self._send(page("слишком большой",
                            "<h1>Материал слишком большой</h1>"
                            f"<p>Предел — {MAX_UPLOAD_BYTES // 1_000_000} МБ.</p>"
                            '<p><a href="./">назад</a></p>'), 413)
            return

        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            fields, uploads = parse_multipart(body, ctype)
        else:
            fields = {k: v[0] for k, v in
                      urllib.parse.parse_qs(body.decode("utf-8")).items()}
            uploads = []

        title = (fields.get("title") or "Без названия").strip()[:120]
        requirement = (fields.get("requirement") or PRESETS[0][1]).strip()[:600]
        rounds = fields.get("rounds", DEFAULT_ROUNDS)
        rounds = rounds if rounds in ("2", "5", "10") else DEFAULT_ROUNDS
        material = (fields.get("material") or "").strip()
        url = (fields.get("url") or "").strip()
        engine = fields.get("engine", "claude-cli")
        if engine not in ENGINES or (ENGINES[engine]["models"] and not openrouter_key()):
            engine = "claude-cli"
        try:
            audience = int(fields.get("audience", "25"))
        except ValueError:
            audience = 25

        if len(material) < 80 and not uploads and not url:
            self._send(page("пусто",
                            "<h1>Нечего анализировать</h1>"
                            "<p>Вставь текст (хотя бы несколько абзацев), "
                            "дай ссылку или приложи файл.</p>"
                            '<p><a href="./">назад</a></p>'), 400)
            return

        job_id = f"{int(time.time())}-{abs(hash(title)) % 10000:04d}"
        work_dir = INBOX_DIR / job_id
        try:
            files, notes = ingest.prepare(
                work_dir, text=material, url=url, uploads=uploads,
                claude_bin=CLAUDE_BIN,
            )
        except ingest.IngestError as exc:
            self._send(page("не вышло",
                            f"<h1>Материал не принят</h1><p>{esc(exc)}</p>"
                            '<p><a href="./">назад</a></p>'), 400)
            return

        requirement, note = ingest.translate_requirement(requirement, CLAUDE_BIN)
        if note:
            notes.append(note)

        self.runner.submit(Job(job_id, title, requirement, rounds, files, notes,
                               engine, audience, fields.get("all_speak") == "1",
                               fields.get("gender", "any"), fields.get("age", "any"),
                               fields.get("brief", "")))
        self.send_response(303)
        self.send_header("Location", f"job/{job_id}")
        self.end_headers()


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, list]:
    """Разбор multipart/form-data на stdlib: модуль cgi из 3.13 удалён."""
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return {}, []
    boundary = ("--" + match.group(1)).encode()

    fields: dict[str, str] = {}
    uploads: list[tuple[str, bytes]] = []
    for part in body.split(boundary):
        if part in (b"", b"--", b"--\r\n") or b"\r\n\r\n" not in part:
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        header = head.decode("utf-8", errors="replace")

        name = re.search(r'name="([^"]*)"', header)
        if not name:
            continue
        filename = re.search(r'filename="([^"]*)"', header)
        if filename:
            if filename.group(1):
                uploads.append((filename.group(1), payload))
        else:
            fields[name.group(1)] = payload.decode("utf-8", errors="replace")
    return fields, uploads


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    if not MIROFISH_BIN.exists():
        log.error("не найден %s", MIROFISH_BIN)
        return 1
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    runner = Runner()
    runner.start()
    Handler.runner = runner
    # Только петля: наружу отдаёт nginx, и только за basic-auth.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("слушаю 127.0.0.1:%d", PORT)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
