#!/usr/bin/env python3
"""
Перевод заключения на русский и казахский.

Движок думает и пишет по-английски — так устроены его промпты. Читать отчёт
по-английски каждый раз неудобно, поэтому сразу после прогона переводим
вердикт и отчёт и кладём рядом. На странице они переключаются кнопкой,
без ожидания.

Перевод делает тот же провайдер, что считал прогон: на подписке — бесплатно,
на OpenRouter — доли цента за страницу текста.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict

log = logging.getLogger("mirofish-translate")

LANGS = {"ru": "Russian", "kk": "Kazakh"}
TIMEOUT = 600
MAX_CHARS = 24000

PROMPT = """Translate the text below into {language}.

Rules:
- keep the Markdown structure exactly: headings, tables, lists, bold, code;
- keep numbers, names, product names and URLs unchanged;
- translate naturally, as a native speaker writing an analytical report —
  not word by word;
- output ONLY the translation, with no preamble and no closing remarks.

TEXT:
{text}"""


def target_dir(state_dir: Path, run_id: str) -> Path:
    return state_dir / "translations" / run_id


def existing(state_dir: Path, run_id: str) -> Dict[str, str]:
    """Готовые переводы: {'ru': '<markdown>', ...}."""
    found: Dict[str, str] = {}
    folder = target_dir(state_dir, run_id)
    if not folder.exists():
        return found
    for lang in LANGS:
        path = folder / f"{lang}.md"
        if path.exists():
            try:
                found[lang] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return found


def _call(text: str, language: str, env: dict, app_dir: Path,
          python_bin: Path) -> str:
    """Один вызов модели через тот же конвейер, что считает прогоны."""
    script = (
        "import sys, json;"
        "sys.path.insert(0, '/opt/mirofish/app');"
        "from app.utils.llm_client import LLMClient;"
        "payload = json.load(sys.stdin);"
        "print(LLMClient().chat("
        "    messages=[{'role': 'user', 'content': payload['prompt']}],"
        "    temperature=0.2, max_tokens=8000))"
    )
    prompt = PROMPT.format(language=language, text=text[:MAX_CHARS])
    # claude и node живут в каталоге приложения: при запуске вне systemd-юнита
    # их нет в PATH, и перевод падал на «No such file or directory: claude».
    env = {**env}
    extra = "/opt/mirofish/node/bin:/opt/mirofish/bin:/opt/mirofish/.local/bin"
    if extra not in env.get("PATH", ""):
        env["PATH"] = extra + ":" + env.get("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", "/opt/mirofish")

    proc = subprocess.run(
        [str(python_bin), "-c", script],
        input=json.dumps({"prompt": prompt}),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(app_dir), env=env, timeout=TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:])
    return (proc.stdout or "").strip()


def build(run_dir: Path, state_dir: Path, run_id: str, env: dict,
          app_dir: Path, python_bin: Path) -> Dict[str, str]:
    """
    Собирает заключение (вердикт + отчёт) и переводит на оба языка.
    Возвращает то, что получилось; неудача по одному языку не ломает второй.
    """
    source = compose_source(run_dir)
    if not source.strip():
        return {}

    folder = target_dir(state_dir, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    done: Dict[str, str] = {}

    for lang, language in LANGS.items():
        path = folder / f"{lang}.md"
        if path.exists():
            done[lang] = path.read_text(encoding="utf-8", errors="replace")
            continue
        try:
            text = _call(source, language, env, app_dir, python_bin)
        except Exception as exc:  # noqa: BLE001 — перевод не критичен для прогона
            log.warning("перевод на %s не удался: %s", lang, exc)
            continue
        if len(text) < 80:
            log.warning("перевод на %s подозрительно короткий", lang)
            continue
        path.write_text(text, encoding="utf-8")
        done[lang] = text
    return done


def compose_source(run_dir: Path) -> str:
    """Заключение одним куском: вердикт человеческим текстом плюс сам отчёт."""
    parts = []
    verdict = run_dir / "report" / "verdict.json"
    if verdict.exists():
        try:
            data = json.loads(verdict.read_text(encoding="utf-8"))
            parts.append("## Verdict\n")
            if data.get("prediction"):
                parts.append(str(data["prediction"]) + "\n")
            if data.get("confidence") is not None:
                parts.append(f"**Confidence:** {data['confidence']}\n")
            for key, title in (("key_dynamics", "Key dynamics"),
                               ("signals", "Signals")):
                items = data.get(key) or []
                if not items:
                    continue
                parts.append(f"\n### {title}\n")
                for item in items:
                    if isinstance(item, dict):
                        label = item.get("signal") or item.get("title") or ""
                        extra = " / ".join(
                            str(item[k]) for k in ("direction", "strength")
                            if item.get(k) is not None)
                        parts.append(f"- {label} ({extra})" if extra else f"- {label}")
                    else:
                        parts.append(f"- {item}")
                parts.append("")
        except (OSError, json.JSONDecodeError):
            pass

    report = run_dir / "report" / "report.md"
    if report.exists():
        try:
            parts.append("\n## Report\n")
            parts.append(report.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)
