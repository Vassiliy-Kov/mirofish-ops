"""
Расширитель аудитории для MiroFish — наш модуль, кладётся в app/services/.

Зачем. Форк делает ровно одного агента на каждую сущность, вытащенную из текста:
оффер StudyRoom дал семь персон, из них половина — сама школа, конкурент и
преподаватели. Спорить в такой толпе почти некому, второй раунд выходит пустым.
Ни смена модели, ни OpenRouter этого не лечат — число агентов задаётся графом.

Что делает. После постройки графа дописывает в него N синтетических людей —
живую аудиторию с разным достатком, возрастом, прошлым опытом и отношением к
предложению. Дальше конвейер работает как обычно: из каждого узла рождается
персона, из персоны — агент.

Метку для новых узлов берём из уже существующих типов графа: фильтр
`filter_defined_entities` отбрасывает всё, чего нет в онтологии, поэтому
выдумывать свой тип нельзя.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger

logger = get_logger("mirofish.audience")

BATCH = 8
MAX_SIZE = 120

# Типы, которые с наибольшей вероятностью означают живого человека, а не компанию.
HUMAN_HINTS = ("person", "parent", "customer", "client", "user", "student",
               "audience", "teacher", "freelancer", "tutor", "individual")

PROMPT = """You are building a synthetic audience for a social simulation.

MATERIAL UNDER TEST:
{material}

SIMULATION QUESTION:
{requirement}

ALREADY IN THE GRAPH (do not duplicate these):
{existing}
{brief}
Invent {count} DISTINCT people who would plausibly encounter this material.
They are the audience, not the vendor.

Make them genuinely different from each other — but any hard requirement stated
above about who belongs in this audience is binding and overrides the spread:
- spread across income levels, ages, family situations and professions;
- different prior experience with this kind of product (none / burned before / happy customer elsewhere);
- different starting stance: some enthusiastic, some neutral, several openly sceptical or hostile;
- different reasons to care, and different things that would make them walk away;
- different ways of speaking (blunt, polite, sarcastic, anxious, analytical, chatty).

At least a third must be sceptics or detractors with concrete, specific objections —
not generic "too expensive".

Return STRICT JSON, no markdown:
{{"people": [
  {{"name": "<short realistic name or handle, in the language of the material>",
    "stance": "supportive|neutral|opposing",
    "summary": "<2-4 sentences IN THE LANGUAGE OF THE MATERIAL: who they are, their
                situation, what they want, what specifically worries them about this
                material, and how they talk>"}}
]}}"""


def material_text(paths: List[str], limit: int = 6000) -> str:
    """Склеивает текстовые исходники прогона. PDF пропускаем — он не читается как текст."""
    chunks: List[str] = []
    for raw in paths or []:
        try:
            path = str(raw)
            if not path.lower().endswith((".md", ".txt", ".markdown")):
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                chunks.append(handle.read())
        except OSError:
            continue
        if sum(len(c) for c in chunks) > limit:
            break
    return "\n\n---\n\n".join(chunks)[:limit]


def _audience_label(nodes: List[Dict[str, Any]]) -> str:
    """Метка для новых узлов: только уже существующий в графе тип пройдёт фильтр."""
    labels: List[str] = []
    for node in nodes:
        raw = node.get("labels") or ([node["label"]] if node.get("label") else [])
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[:,|]", raw) if part]
        labels.extend(lbl.strip() for lbl in raw if lbl and lbl.strip() not in ("Entity", "Node"))

    for label in labels:
        if any(hint in label.lower() for hint in HUMAN_HINTS):
            return label
    if labels:
        # Самый частый тип — он точно описан в онтологии.
        return max(set(labels), key=labels.count)
    return "Person"


def _parse_people(payload: Any) -> List[Dict[str, str]]:
    people = payload.get("people") if isinstance(payload, dict) else payload
    if not isinstance(people, list):
        return []
    cleaned = []
    for person in people:
        if not isinstance(person, dict):
            continue
        name = str(person.get("name", "")).strip()[:60]
        summary = str(person.get("summary", "")).strip()
        if not name or len(summary) < 30:
            continue
        stance = str(person.get("stance", "neutral")).strip().lower()
        cleaned.append({
            "name": name,
            "summary": summary,
            "stance": stance if stance in ("supportive", "neutral", "opposing") else "neutral",
        })
    return cleaned


def expand_audience(graph_id: str, requirement: str, material: str,
                    size: int | None = None, brief: str | None = None) -> int:
    """Дописывает в граф синтетическую аудиторию. Возвращает число добавленных."""
    if size is None:
        size = int(os.environ.get("MIROFISH_AUDIENCE_SIZE", "0") or 0)
    size = max(0, min(int(size), MAX_SIZE))
    if size == 0:
        return 0

    # Жёсткий отбор сегмента: «только женщины 26–35» и прочие условия от Василия.
    if brief is None:
        brief = os.environ.get("MIROFISH_AUDIENCE_BRIEF", "")
    brief = (brief or "").strip()
    brief_block = (
        f"\nHARD REQUIREMENT — every person you invent must match this audience: "
        f"{brief}. Do not include anyone outside it.\n" if brief else "\n"
    )

    from .graph_db import GraphDatabase

    db = GraphDatabase()
    graph = db.get_graph_data(graph_id) or {}
    nodes = graph.get("nodes") or []
    label = _audience_label(nodes)
    existing_names = {str(n.get("name", "")).strip().lower() for n in nodes}

    existing_block = "\n".join(
        f"- {n.get('name')}: {str(n.get('summary', ''))[:160]}" for n in nodes[:20]
    ) or "(graph is empty)"

    # Без исходного текста описываем предмет сводкой графа — иначе модель
    # придумает аудиторию для чего-то своего.
    if len(material.strip()) < 200:
        material = existing_block

    client = LLMClient()
    added = 0
    attempts = 0
    # Батчами: длинный JSON модели любят обрывать на середине.
    while added < size and attempts < (size // BATCH) + 4:
        attempts += 1
        want = min(BATCH, size - added)
        try:
            payload = client.chat_json(
                [{"role": "user", "content": PROMPT.format(
                    material=material[:6000],
                    requirement=requirement[:600],
                    existing=existing_block,
                    brief=brief_block,
                    count=want,
                )}],
                temperature=0.95,   # высокая — нам нужен разброс, а не аккуратность
                max_tokens=3000,
            )
        except Exception as exc:  # noqa: BLE001 — одна неудачная пачка не рушит прогон
            logger.warning(f"audience batch failed: {exc}")
            continue

        for person in _parse_people(payload):
            if person["name"].lower() in existing_names:
                continue
            try:
                db.add_node(
                    graph_id=graph_id,
                    name=person["name"],
                    labels=["Entity", label],
                    summary=person["summary"],
                    attributes={"synthetic_audience": True, "stance": person["stance"]},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"audience node rejected: {exc}")
                continue
            existing_names.add(person["name"].lower())
            added += 1
            if added >= size:
                break

        logger.info(f"audience: {added}/{size} added (batch {attempts})")

    if added:
        logger.info(f"audience expanded: +{added} people as '{label}'")
    else:
        logger.warning("audience expansion produced nothing")
    return added
