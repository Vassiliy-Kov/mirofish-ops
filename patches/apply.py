#!/usr/bin/env python3
"""
Врезки OpenRouter в форк amadad/mirofish.

Форк — чужой AGPL-код, поэтому правок минимум: один наш модуль
(`app/utils/openrouter_model.py`) плюс три короткие врезки, каждая помечена
маркером и накладывается повторно без вреда. Обновили форк — прогнали заново.

    python3 apply.py /opt/mirofish/app          # наложить
    python3 apply.py /opt/mirofish/app --check  # только проверить
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARK = "# [mirofish-ops]"

PATCHES: list[tuple[str, str, str]] = [
    # --- config.py: окружение сильнее файла ---------------------------------
    # load_dotenv(override=True) затирал LLM_PROVIDER, переданный процессу, и
    # выбор движка в интерфейсе не доезжал: всё считалось подпиской, даже когда
    # выбран OpenRouter. Теперь явно переданное окружение восстанавливается.
    (
        "app/config.py",
        '''def _load_environment() -> None:
    project_root_env = os.path.join(os.path.dirname(__file__), "../.env")
    if os.path.exists(project_root_env):
        load_dotenv(project_root_env, override=True)
        return

    load_dotenv(override=True)''',
        f'''def _load_environment() -> None:
    {MARK} снимок того, что задано процессу явно — файл не должен это затирать
    _explicit = {{
        key: value for key, value in os.environ.items()
        if key.startswith(("LLM_", "OPENROUTER_", "MIROFISH_", "OASIS_"))
    }}

    project_root_env = os.path.join(os.path.dirname(__file__), "../.env")
    if os.path.exists(project_root_env):
        load_dotenv(project_root_env, override=True)
    else:
        load_dotenv(override=True)

    os.environ.update(_explicit)''',
    ),
    # --- config.py: разрешить провайдера и проверить ключ -------------------
    (
        "app/config.py",
        '''        if cls.LLM_PROVIDER not in ("claude-cli", "codex-cli"):
            errors.append(f"LLM_PROVIDER must be 'claude-cli' or 'codex-cli', got '{cls.LLM_PROVIDER}'")''',
        f'''        {MARK} openrouter добавлен как третий провайдер
        if cls.LLM_PROVIDER not in ("claude-cli", "codex-cli", "openrouter", "openai-compatible"):
            errors.append(
                "LLM_PROVIDER must be 'claude-cli', 'codex-cli' or 'openrouter', "
                f"got '{{cls.LLM_PROVIDER}}'"
            )
        if cls.LLM_PROVIDER in ("openrouter", "openai-compatible") and not (
            os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")
        ):
            errors.append("OPENROUTER_API_KEY (or LLM_API_KEY) is required for openrouter")''',
    ),
    # --- llm_client.py: разрешить провайдера --------------------------------
    (
        "app/utils/llm_client.py",
        '''        if self.provider not in ("claude-cli", "codex-cli"):
            raise ValueError(f"Unsupported LLM provider: {self.provider!r}. Use 'claude-cli' or 'codex-cli'.")''',
        f'''        {MARK} openrouter добавлен как третий провайдер
        if self.provider not in ("claude-cli", "codex-cli", "openrouter", "openai-compatible"):
            raise ValueError(
                f"Unsupported LLM provider: {{self.provider!r}}. "
                "Use 'claude-cli', 'codex-cli' or 'openrouter'."
            )''',
    ),
    # --- llm_client.py: ветка вызова ----------------------------------------
    (
        "app/utils/llm_client.py",
        '''                if self.provider == "codex-cli":
                    return self._chat_codex_cli(messages, temperature, max_tokens, response_format)
                return self._chat_claude_cli(messages, temperature, max_tokens, response_format)''',
        f'''                {MARK} ветка на OpenAI-совместимый endpoint
                if self.provider in ("openrouter", "openai-compatible"):
                    from .openrouter_model import chat_completion
                    return chat_completion(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=bool(response_format
                                       and response_format.get("type") == "json_object"),
                    )
                if self.provider == "codex-cli":
                    return self._chat_codex_cli(messages, temperature, max_tokens, response_format)
                return self._chat_claude_cli(messages, temperature, max_tokens, response_format)''',
    ),
    # --- oasis_llm.py: модели для симуляции ---------------------------------
    (
        "app/utils/oasis_llm.py",
        """    model = config.get('llm_model') or provider

    logger.info(f"OASIS model: provider={provider}, model={model}, mode=cli-bridge")""",
        f"""    {MARK} смесь моделей: OASIS раздаёт список агентам по кругу
    if provider in ('openrouter', 'openai-compatible'):
        from .openrouter_model import build_models, configured_models
        names = configured_models(config)
        logger.info(f"OASIS models: provider={{provider}}, mix={{names}}")
        return build_models(config, use_boost=use_boost)

    model = config.get('llm_model') or provider

    logger.info(f"OASIS model: provider={{provider}}, model={{model}}, mode=cli-bridge")""",
    ),
    # --- oasis_llm.py: параллелизм ------------------------------------------
    (
        "app/utils/oasis_llm.py",
        '''    """Get CLI-appropriate OASIS concurrency limit."""
    return int(os.environ.get('OASIS_CLI_SEMAPHORE', str(DEFAULT_CLI_SEMAPHORE)))''',
        f'''    """Get CLI-appropriate OASIS concurrency limit."""
    {MARK} у API нет узкого горла CLI — можно считать агентов параллельно
    provider = (os.environ.get('LLM_PROVIDER') or Config.LLM_PROVIDER or '').lower()
    if provider in ('openrouter', 'openai-compatible'):
        return int(os.environ.get('OASIS_API_SEMAPHORE', '12'))
    return int(os.environ.get('OASIS_CLI_SEMAPHORE', str(DEFAULT_CLI_SEMAPHORE)))''',
    ),
    # --- run_parallel_simulation.py: пустить в раунд почти всех -------------
    # Движок по умолчанию активирует 2–14 агентов в час, режет уровнем активности
    # и ночными множителями: из 25 персон заговорили восемь. При MIROFISH_ALL_SPEAK
    # берём всех, кто вообще бодрствует в этот час.
    (
        "app/scripts/run_parallel_simulation.py",
        # Внимание: в оригинале пустые строки внутри цикла содержат отступ
        # из восьми пробелов — без них врезка не находит своё место.
        "    candidates = []\n"
        "    for cfg in agent_configs:\n"
        '        agent_id = cfg.get("agent_id", 0)\n'
        '        active_hours = cfg.get("active_hours", list(range(8, 23)))\n'
        '        activity_level = cfg.get("activity_level", 0.5)\n'
        "        \n"
        "        if current_hour not in active_hours:\n"
        "            continue\n"
        "        \n"
        "        if random.random() < activity_level:\n"
        "            candidates.append(agent_id)",
        f"""    {MARK} режим «говорят почти все»
    _all_speak = os.environ.get("MIROFISH_ALL_SPEAK", "").strip().lower() in ("1", "true", "yes")

    candidates = []
    for cfg in agent_configs:
        agent_id = cfg.get("agent_id", 0)
        active_hours = cfg.get("active_hours", list(range(8, 23)))
        activity_level = cfg.get("activity_level", 0.5)

        if _all_speak:
            # Часы игнорируем: короткому прогону незачем ждать утра.
            # Порог низкий, чтобы совсем молчаливые всё же изредка пропускали ход.
            if random.random() < max(activity_level, 0.9):
                candidates.append(agent_id)
            continue

        if current_hour not in active_hours:
            continue

        if random.random() < activity_level:
            candidates.append(agent_id)

    if _all_speak:
        target_count = len(candidates)

    if round_num == 0:
        # Видно в simulation.log: дошёл ли режим и сколько людей он пустил.
        print(f"[mirofish-ops] all_speak={{_all_speak}} candidates={{len(candidates)}} "
              f"target={{target_count}} configs={{len(agent_configs)}}", flush=True)""",
    ),
    # --- cli.py: синтетическая аудитория между графом и профилями ------------
    (
        "app/cli.py",
        '''        store.update(run_id, graph_id=graph_id, status="graph_ready", task_progress=100, task_message="Graph ready")''',
        f'''        store.update(run_id, graph_id=graph_id, status="graph_ready", task_progress=100, task_message="Graph ready")

        {MARK} синтетическая аудитория: форк делает одного агента на сущность,
        # поэтому без этого шага спорить в симуляции почти некому.
        _audience_size = int(os.environ.get("MIROFISH_AUDIENCE_SIZE", "0") or 0)
        if _audience_size > 0:
            try:
                from .services.audience_expander import expand_audience, material_text
                display.update_step("graph", f"Generating audience ({{_audience_size}})")
                _added = expand_audience(
                    graph_id=graph_id,
                    requirement=args.requirement,
                    material=material_text(source_files),
                    size=_audience_size,
                )
                if _added:
                    graph_data = graph_builder.get_graph_data(graph_id)
                    graph_stats = graph_db.get_graph_statistics(graph_id)
                    n_nodes = graph_stats.get("node_count", n_nodes)
                    n_edges = graph_stats.get("edge_count", n_edges)
                    display.complete_step(
                        "graph", f"{{n_nodes}} nodes (+{{_added}} audience), {{n_edges}} edges"
                    )
            except Exception as _audience_exc:  # noqa: BLE001
                logger.warning(f"audience expansion skipped: {{_audience_exc}}")''',
    ),
]

AUDIENCE_MODULE = ("audience_expander.py", "app/services/audience_expander.py")

# config.py не импортирует os? Импортирует — проверяем на всякий случай.
IMPORT_GUARD = ("app/config.py", "import os")


MODULES = [
    ("openrouter_model.py", "app/utils/openrouter_model.py"),
    ("audience_expander.py", "app/services/audience_expander.py"),
]


def apply(app_dir: Path, check_only: bool = False) -> int:
    problems: list[str] = []
    changed = 0

    # 1. Наши модули
    for src_name, rel_dst in MODULES:
        module_src = Path(__file__).with_name(src_name)
        module_dst = app_dir / rel_dst
        if check_only:
            if not module_dst.exists():
                problems.append(f"нет {rel_dst}")
            elif module_dst.read_text(encoding="utf-8") != module_src.read_text(encoding="utf-8"):
                problems.append(f"{src_name} отличается от исходника")
        else:
            module_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(module_src, module_dst)
            print(f"  модуль → {module_dst}")

    # 2. Врезки
    for rel, old, new in PATCHES:
        path = app_dir / rel
        if not path.exists():
            problems.append(f"нет файла {rel}")
            continue
        text = path.read_text(encoding="utf-8")

        if new in text:
            print(f"  {rel}: уже на месте")
            continue
        if old not in text:
            problems.append(
                f"{rel}: не нашёл место для врезки — форк изменился, патч надо обновить"
            )
            continue
        if check_only:
            problems.append(f"{rel}: врезка не наложена")
            continue

        backup = path.with_suffix(path.suffix + ".orig")
        if not backup.exists():
            shutil.copyfile(path, backup)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"  {rel}: врезка наложена")
        changed += 1

    # 3. Страховка по импорту
    guard_path = app_dir / IMPORT_GUARD[0]
    if guard_path.exists() and IMPORT_GUARD[1] not in guard_path.read_text(encoding="utf-8"):
        problems.append(f"{IMPORT_GUARD[0]}: нет '{IMPORT_GUARD[1]}', врезка не сработает")

    if problems:
        print("\nПРОБЛЕМЫ:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\nOK: врезок наложено {changed}, остальное уже было на месте")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    app_dir = Path(sys.argv[1])
    if not (app_dir / "app").is_dir():
        print(f"не похоже на каталог форка: {app_dir}", file=sys.stderr)
        return 2
    return apply(app_dir, check_only="--check" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
