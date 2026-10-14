#!/usr/bin/env python3
"""
Пиксельная сцена прогона — то, чего нет в текстовом отчёте.

Отчёт говорит, ЧТО аудитория подумала. Сцена показывает, КАК это случилось:
кто заговорил первым, в каком раунде подхватили, кто молчал всю симуляцию.
Рисуется из настоящего журнала (`simulation/actions.jsonl`), ничего не выдумано.

Зависимостей нет: данные отдаются в HTML как JSON, рисует canvas на месте.
"""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

MAX_ACTIONS = 4000
BIO_LIMIT = 400


def load_scene(run_dir: Path) -> Dict[str, Any]:
    """Собирает агентов и их действия по раундам."""
    actions_path = run_dir / "simulation" / "actions.jsonl"
    if not actions_path.exists():
        return {"agents": [], "rounds": [], "error": "журнал симуляции не найден"}

    bios = _load_bios(run_dir)
    agents: Dict[int, Dict[str, Any]] = {}
    by_round: Dict[int, List[Dict[str, Any]]] = {}

    with actions_path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_ACTIONS:
                break
            line = line.strip()
            if not line:
                continue
            try:
                action = json.loads(line)
            except json.JSONDecodeError:
                continue

            agent_id = action.get("agent_id")
            if agent_id is None:
                continue
            name = str(action.get("agent_name") or f"Агент {agent_id}")
            if agent_id not in agents:
                bio = bios.get(name) or bios.get(str(agent_id)) or ""
                agents[agent_id] = {
                    "id": agent_id,
                    "name": name,
                    "bio": bio[:BIO_LIMIT],
                    "stance": _stance(bio),
                    "platform": action.get("platform", "twitter"),
                    "acts": 0,
                }
            agents[agent_id]["acts"] += 1

            args = action.get("action_args") or {}
            content = args.get("content") or args.get("comment") or ""
            round_num = int(action.get("round_num") or 0)
            by_round.setdefault(round_num, []).append({
                "agent": agent_id,
                "type": action.get("action_type", "ACTION"),
                "platform": action.get("platform", "twitter"),
                "text": str(content)[:1200],
            })

    rounds = [by_round.get(num, []) for num in range(max(by_round) + 1)] if by_round else []
    ordered = sorted(agents.values(), key=lambda a: (-a["acts"], a["id"]))
    return {"agents": ordered, "rounds": rounds, "error": None}


def load_live_scene(sim_dir: Path) -> Dict[str, Any]:
    """
    То же самое, но по ходу прогона: движок пишет действия в
    uploads/simulations/<sim_id>/{twitter,reddit}/actions.jsonl ещё до того,
    как соберёт итоговый отчёт. Формат там другой — строки событий
    (round_start / действие / round_end) вперемешку.
    """
    bios = _load_bios(sim_dir)          # профили лежат прямо в sim_dir
    agents: Dict[int, Dict[str, Any]] = {}
    by_round: Dict[int, List[Dict[str, Any]]] = {}

    for platform in ("twitter", "reddit"):
        path = sim_dir / platform / "actions.jsonl"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for line in lines[:MAX_ACTIONS]:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Служебные события раунда нам не нужны — только сами реплики.
            if event.get("event_type") or event.get("agent_id") is None:
                continue

            agent_id = event["agent_id"]
            name = str(event.get("agent_name") or f"Агент {agent_id}")
            if agent_id not in agents:
                bio = bios.get(name, "")
                agents[agent_id] = {
                    "id": agent_id, "name": name, "bio": bio[:BIO_LIMIT],
                    "stance": _stance(bio), "platform": platform, "acts": 0,
                }
            agents[agent_id]["acts"] += 1

            args = event.get("action_args") or {}
            by_round.setdefault(int(event.get("round") or 0), []).append({
                "agent": agent_id,
                "type": event.get("action_type", "ACTION"),
                "platform": platform,
                "text": str(args.get("content") or args.get("comment") or "")[:1200],
            })

    rounds = [by_round.get(num, []) for num in range(max(by_round) + 1)] if by_round else []
    ordered = sorted(agents.values(), key=lambda a: (-a["acts"], a["id"]))
    return {"agents": ordered, "rounds": rounds, "error": None}


def _load_bios(run_dir: Path) -> Dict[str, str]:
    """Описания персон: нужны, чтобы при клике было видно, кто это."""
    bios: Dict[str, str] = {}
    # В готовом прогоне профили лежат в simulation/, в живом — в корне каталога.
    sim = run_dir / "simulation"
    if not sim.exists():
        sim = run_dir

    csv_path = sim / "twitter_profiles.csv"
    if csv_path.exists():
        try:
            with csv_path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("name") or "").strip()
                    if name:
                        bios[name] = (row.get("user_char") or row.get("description") or "").strip()
        except (OSError, csv.Error):
            pass

    json_path = sim / "reddit_profiles.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            for item in data if isinstance(data, list) else []:
                name = str(item.get("realname") or item.get("name") or "").strip()
                if name and name not in bios:
                    bios[name] = str(item.get("persona") or item.get("bio") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass
    return bios


def _stance(bio: str) -> str:
    """
    Позиция участника из его же описания.

    Генератор персон часто пишет её прямым текстом — «Her stance is neutral to
    sceptical» — поэтому сначала ищем явную формулировку и только потом
    опускаемся до ключевых слов по всему тексту.
    """
    low = bio.lower()

    explicit = re.search(r"stance[^.]{0,40}?\b(is|:)\s*([a-z ,\-]{3,40})", low)
    if explicit:
        said = explicit.group(2)
        # «neutral to sceptical» — это скорее против, поэтому скепсис весомее.
        if any(w in said for w in ("sceptic", "skeptic", "critical", "opposing",
                                   "hostile", "negative", "against")):
            return "opposing"
        if any(w in said for w in ("supportive", "positive", "enthusiastic",
                                   "advocate", "favourable", "favorable")):
            return "supportive"
        if "neutral" in said:
            return "neutral"

    if any(word in low for word in ("sceptic", "skeptic", "opposing", "critic",
                                    "hostile", "distrust", "burned by",
                                    "скепти", "против", "недовер")):
        return "opposing"
    if any(word in low for word in ("supportive", "enthusias", "advocate",
                                    "recommends", "сторонник", "довол", "рекоменду")):
        return "supportive"
    return "neutral"


def stance_breakdown(agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Доли позиций в зале — в людях и процентах."""
    total = len(agents) or 1
    titles = {"supportive": "скорее за", "neutral": "нейтральны",
              "opposing": "скорее против"}
    colors = {"supportive": "#2f8f5f", "neutral": "#8a8a84", "opposing": "#b4493a"}
    counted = Counter(a["stance"] for a in agents)
    return [{
        "key": key, "title": titles[key], "color": colors[key],
        "count": counted.get(key, 0),
        "percent": round(counted.get(key, 0) * 100 / total),
    } for key in ("supportive", "neutral", "opposing")]


def donut_svg(rows: List[Dict[str, Any]], total: int) -> str:
    """Кольцевая диаграмма без библиотек: три дуги по длине обводки."""
    radius, stroke = 54, 18
    circumference = 2 * 3.14159 * radius
    offset = 0.0
    arcs = []
    for row in rows:
        if not row["count"]:
            continue
        length = circumference * row["count"] / max(total, 1)
        arcs.append(
            f'<circle cx="70" cy="70" r="{radius}" fill="none" '
            f'stroke="{row["color"]}" stroke-width="{stroke}" '
            f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 70 70)"/>'
        )
        offset += length
    return (
        f'<svg viewBox="0 0 140 140" width="140" height="140" role="img" '
        f'aria-label="расклад зала">'
        f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="var(--line)" '
        f'stroke-width="{stroke}"/>{"".join(arcs)}'
        f'<text x="70" y="66" text-anchor="middle" font-size="22" font-weight="600" '
        f'fill="currentColor">{total}</text>'
        f'<text x="70" y="84" text-anchor="middle" font-size="11" '
        f'fill="currentColor" opacity=".7">человек</text></svg>'
    )


# --------------------------------------------------------------------- html

SCENE_CSS = """
.scene-wrap{margin:0 0 18px}
#stage{width:100%;height:auto;display:block;border:1px solid var(--line);
border-radius:12px;background:var(--card);image-rendering:pixelated;cursor:pointer}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
.controls button{margin:0;padding:8px 16px;font-size:14px}
.controls input[type=range]{flex:1;min-width:160px;accent-color:var(--accent)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-top:8px}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
#said{max-height:320px;overflow-y:auto;margin-top:12px}
#said .line{padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
#said .who{font-weight:600}
#said .plat{color:var(--muted);font-size:12px;margin-left:6px}
.stage-row{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}
.stage-col{flex:1 1 420px;min-width:0}
.tally{flex:0 0 212px;border:1px solid var(--line);border-radius:10px;padding:14px}
.tally h3{margin:0 0 10px;font-size:14px}
.tally .ring{display:flex;justify-content:center;margin-bottom:12px;color:var(--ink)}
.tally .row{margin-bottom:10px;font-size:13px;overflow:hidden}
.tally .row b{float:right;font-variant-numeric:tabular-nums}
.tally .track{height:7px;border-radius:99px;background:var(--bg);
border:1px solid var(--line);overflow:hidden;margin-top:4px}
.tally .track i{display:block;height:100%}
.tally .foot{font-size:12px;color:var(--muted);border-top:1px solid var(--line);
padding-top:10px;margin-top:4px}
@media(max-width:780px){.tally{flex:1 1 100%}}
"""

SCENE_JS_OLD = r"""
const DATA = __DATA__;
const cv = document.getElementById('stage');
const ctx = cv.getContext('2d');
const said = document.getElementById('said');
const slider = document.getElementById('slider');
const playBtn = document.getElementById('play');
const label = document.getElementById('label');

const CELL = 86, PAD = 26, SCALE = 4;
let round = 0, playing = false, timer = null, picked = null;

// Пиксельный человечек 11x14. 0 — пусто, 1 волосы, 2 кожа, 3 одежда, 4 обводка.
const SPRITE = [
  "00011111000","00111111100","01144441100","01422224110","01422224110",
  "01222222210","00122222100","00013331000","00133333100","01333333310",
  "01333333310","01333333310","00133033100","00330033000",
];

// Цвет из имени — чтобы персонаж был узнаваем между раундами.
function hash(str){let h=0;for(const ch of str)h=(h*31+ch.charCodeAt(0))|0;return Math.abs(h);}
const HAIR = ['#3b2a1e','#6b4423','#1f1a17','#8a6a3a','#52301d','#2c2c34','#7a4a2a'];
const SKIN = ['#f1c9a0','#e0ab7f','#c98f63','#9c6b4a','#f6d7b8','#7d5236'];
const WEAR = ['#4a7ba7','#7a5a9c','#4f8f6a','#b2694a','#56606e','#9a5577','#3f6f8f'];

function palette(agent){
  const h = hash(agent.name);
  return {hair: HAIR[h % HAIR.length], skin: SKIN[(h >> 3) % SKIN.length],
          wear: WEAR[(h >> 6) % WEAR.length]};
}
const EDGE = {supportive:'#2f8f5f', opposing:'#b4493a', neutral:'#8a8a84'};

const TOP = 150;   // запас сверху: над головой первого ряда встаёт пузырь

function layout(){
  const perRow = Math.max(3, Math.floor((cv.width - PAD * 2) / CELL));
  return DATA.agents.map((a, i) => ({
    agent: a,
    x: PAD + (i % perRow) * CELL + CELL / 2,
    y: TOP + Math.floor(i / perRow) * (CELL + 30),
  }));
}

function resize(){
  // Внутренний размер = CSS-размеру: пиксель-арт не должен размываться
  // под devicePixelRatio, поэтому никакого масштабирования контекста.
  const w = Math.max(320, cv.parentElement.clientWidth);
  const perRow = Math.max(3, Math.floor((w - PAD * 2) / CELL));
  const rows = Math.ceil(DATA.agents.length / perRow);
  cv.width = w;
  cv.height = TOP + 26 + (rows - 1) * (CELL + 30);
  cv.style.width = '100%';
  cv.style.height = 'auto';
  draw();
}

function drawPerson(x, y, agent, active){
  const p = palette(agent);
  const colors = {1: p.hair, 2: p.skin, 3: p.wear, 4: '#00000055'};
  const w = SPRITE[0].length * SCALE, h = SPRITE.length * SCALE;
  const left = Math.round(x - w / 2), top = Math.round(y - h);

  if (active){
    ctx.fillStyle = '#f2c14e33';
    ctx.fillRect(left - 8, top - 8, w + 16, h + 16);
  }
  ctx.imageSmoothingEnabled = false;
  SPRITE.forEach((row, ry) => {
    [...row].forEach((ch, rx) => {
      if (ch === '0') return;
      ctx.fillStyle = colors[ch] || p.wear;
      ctx.fillRect(left + rx * SCALE, top + ry * SCALE, SCALE, SCALE);
    });
  });

  ctx.strokeStyle = EDGE[agent.stance] || EDGE.neutral;
  ctx.lineWidth = 2;
  ctx.strokeRect(left - 5.5, top - 5.5, w + 11, h + 11);

  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.font = '12px system-ui, sans-serif';
  ctx.textAlign = 'center';
  const name = agent.name.length > 14 ? agent.name.slice(0, 13) + '…' : agent.name;
  ctx.fillText(name, x, y + 16);
}

function speechMark(x, y){
  const top = y - SPRITE.length * SCALE - 20;
  ctx.fillStyle = '#f2c14e';
  ctx.beginPath();
  ctx.roundRect(x + 14, top - 6, 18, 13, 3);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(x + 18, top + 7); ctx.lineTo(x + 24, top + 7); ctx.lineTo(x + 18, top + 12);
  ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#2a2a26';
  [0, 5, 10].forEach(dx => ctx.fillRect(x + 17 + dx, top, 2, 2));
}

function bubble(x, y, text){
  const words = text.replace(/\s+/g, ' ').trim().slice(0, 90);
  if (!words) return;
  ctx.font = '11px system-ui, sans-serif';
  const lines = [];
  let line = '';
  for (const word of words.split(' ')){
    if ((line + ' ' + word).length > 24){ lines.push(line); line = word; }
    else line = line ? line + ' ' + word : word;
    if (lines.length >= 3) break;
  }
  if (line && lines.length < 3) lines.push(line);
  const w = 190, h = 14 * lines.length + 14;
  const bx = Math.min(Math.max(x - w / 2, 4), cv.width - w - 4);
  // 46 — нижняя граница строки «Раунд N», её пузырь перекрывать не должен.
  const by = Math.max(46, y - SPRITE.length * SCALE - 34 - h);

  ctx.fillStyle = getComputedStyle(cv).backgroundColor;
  ctx.strokeStyle = EDGE.neutral;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(bx, by, w, h, 6);
  ctx.fill(); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x - 5, by + h); ctx.lineTo(x + 5, by + h); ctx.lineTo(x, by + h + 8);
  ctx.closePath(); ctx.fill();

  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.textAlign = 'left';
  lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 17 + i * 14));
}

function draw(){
  ctx.clearRect(0, 0, cv.width, cv.height);
  const acts = DATA.rounds[round] || [];
  const speaking = new Map();
  acts.forEach(a => { if (!speaking.has(a.agent)) speaking.set(a.agent, a); });

  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.font = '600 15px system-ui, sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText(`Раунд ${round + 1} из ${DATA.rounds.length} · высказались ${speaking.size} из ${DATA.agents.length}`,
               PAD, 34);

  const spots = layout();
  spots.forEach(s => drawPerson(s.x, s.y, s.agent, speaking.has(s.agent.id)));

  // Значок речи — у всех, кто говорил. Текст — только у одного, иначе
  // пузыри наезжают друг на друга и читать нечего.
  spots.forEach(s => { if (speaking.has(s.agent.id)) speechMark(s.x, s.y); });

  const focus = picked !== null && speaking.has(picked)
    ? spots.find(s => s.agent.id === picked)
    : spots.find(s => speaking.has(s.agent.id));
  if (focus) bubble(focus.x, focus.y, speaking.get(focus.agent.id).text);

  renderSaid(acts);
}

function renderSaid(acts){
  if (!acts.length){
    said.innerHTML = '<p class="hint">В этом раунде никто не заговорил.</p>';
    return;
  }
  const shown = picked === null ? acts : acts.filter(a => a.agent === picked);
  said.innerHTML = shown.map(a => {
    const agent = DATA.agents.find(x => x.id === a.agent) || {name: 'Агент'};
    return `<div class="line"><span class="who">${esc(agent.name)}</span>` +
           `<span class="plat">${esc(a.platform)} · ${esc(a.type)}</span><br>${esc(a.text)}</div>`;
  }).join('') || '<p class="hint">Этот участник в раунде промолчал.</p>';
}

function esc(s){
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

cv.addEventListener('click', ev => {
  const rect = cv.getBoundingClientRect();
  const x = (ev.clientX - rect.left) * (cv.width / rect.width);
  const y = (ev.clientY - rect.top) * (cv.height / rect.height);
  const hit = layout().find(s => Math.abs(s.x - x) < 34 && y > s.y - 70 && y < s.y + 20);
  picked = hit && picked !== hit.agent.id ? hit.agent.id : null;
  label.textContent = picked === null ? ''
    : (DATA.agents.find(a => a.id === picked)?.bio || 'описание не сохранилось');
  draw();
});

slider.addEventListener('input', () => { round = +slider.value; draw(); });
playBtn.addEventListener('click', () => {
  playing = !playing;
  playBtn.textContent = playing ? '❚❚ Пауза' : '▶ Проиграть';
  clearInterval(timer);
  if (playing){
    timer = setInterval(() => {
      round = (round + 1) % DATA.rounds.length;
      slider.value = round;
      draw();
      if (round === DATA.rounds.length - 1){
        playing = false; playBtn.textContent = '▶ Проиграть'; clearInterval(timer);
      }
    }, 2200);
  }
});

// На бумаге листать нечего: перед печатью разворачиваем ВЕСЬ журнал,
// иначе в PDF уедет только текущий раунд.
window.addEventListener('beforeprint', () => {
  const all = DATA.rounds.map((acts, i) => {
    if (!acts.length) return `<div class="line"><i>Раунд ${i + 1}: никто не заговорил.</i></div>`;
    return `<h3>Раунд ${i + 1}</h3>` + acts.map(a => {
      const agent = DATA.agents.find(x => x.id === a.agent) || {name: 'Агент'};
      return `<div class="line"><span class="who">${esc(agent.name)}</span>` +
             `<span class="plat">${esc(a.platform)} · ${esc(a.type)}</span><br>${esc(a.text)}</div>`;
    }).join('');
  }).join('');
  said.innerHTML = all;
});
window.addEventListener('afterprint', draw);

window.addEventListener('resize', resize);
resize();
"""


SCENE_JS = r"""
const DATA = __DATA__;
const cv = document.getElementById('stage');
const ctx = cv.getContext('2d');
const said = document.getElementById('said');
const slider = document.getElementById('slider');
const playBtn = document.getElementById('play');
const label = document.getElementById('label');

const SCALE = 4;
// TOP с запасом: над дальним рядом должны поместиться пузырь и подпись.
const TABLE_W = 216, TABLE_H = 104, GAP_X = 46, GAP_Y = 126, PAD = 28, TOP = 258;
const SEATS_PER_TABLE = 6;
let round = 0, playing = false, timer = null, picked = null, tick = 0, raf = null;

// Пиксельный человек, 11x14. 0 пусто, 1 волосы, 2 кожа, 3 одежда, 4 обводка.
const SPRITE = [
  "00011111000","00111111100","01144441100","01422224110","01422224110",
  "01222222210","00122222100","00013331000","00133333100","01333333310",
  "01333333310","01333333310","00133033100","00330033000",
];

function hash(str){let h=0;for(const ch of str)h=(h*31+ch.charCodeAt(0))|0;return Math.abs(h);}
const HAIR = ['#3b2a1e','#6b4423','#1f1a17','#8a6a3a','#52301d','#2c2c34','#7a4a2a'];
const SKIN = ['#f1c9a0','#e0ab7f','#c98f63','#9c6b4a','#f6d7b8','#7d5236'];
const WEAR = ['#4a7ba7','#7a5a9c','#4f8f6a','#b2694a','#56606e','#9a5577','#3f6f8f'];
const EDGE = {supportive:'#2f8f5f', opposing:'#b4493a', neutral:'#8a8a84'};
const STANCE_ORDER = {supportive: 0, neutral: 1, opposing: 2};

function palette(agent){
  const h = hash(agent.name);
  return {hair: HAIR[h % HAIR.length], skin: SKIN[(h >> 3) % SKIN.length],
          wear: WEAR[(h >> 6) % WEAR.length]};
}

// Рассадка: соседи по столу — люди близкой позиции, чтобы было видно,
// где собрались скептики, а где сторонники.
const SEATED = (() => {
  const sorted = [...DATA.agents].sort((a, b) =>
    (STANCE_ORDER[a.stance] ?? 1) - (STANCE_ORDER[b.stance] ?? 1) || a.id - b.id);
  const tables = [];
  for (let i = 0; i < sorted.length; i += SEATS_PER_TABLE){
    tables.push(sorted.slice(i, i + SEATS_PER_TABLE));
  }
  return tables;
})();

function tableCols(){
  return Math.max(1, Math.floor((cv.width - PAD * 2 + GAP_X) / (TABLE_W + GAP_X)));
}

// Шесть мест вокруг стола: трое за дальним краем (спиной к нам видны затылки),
// трое у ближнего. dy — положение НОГ: дальние уходят под столешницу,
// ближние стоят перед ней и рисуются поверх стола.
function seatOffsets(n){
  const far = -TABLE_H / 2 + 16, near = TABLE_H / 2 + 58;
  const spots = [
    {dx: -72, dy: far,  back: true},  {dx: 0, dy: far - 6, back: true},
    {dx: 72,  dy: far,  back: true},
    {dx: -72, dy: near, back: false}, {dx: 0, dy: near + 6, back: false},
    {dx: 72,  dy: near, back: false},
  ];
  return spots.slice(0, n);
}

function layout(){
  const cols = tableCols();
  const spots = [];
  SEATED.forEach((table, ti) => {
    const cx = PAD + (ti % cols) * (TABLE_W + GAP_X) + TABLE_W / 2;
    const cy = TOP + Math.floor(ti / cols) * (TABLE_H + GAP_Y) + TABLE_H / 2;
    seatOffsets(table.length).forEach((seat, si) => {
      spots.push({agent: table[si], x: cx + seat.dx, y: cy + seat.dy,
                  back: seat.back, table: ti, tx: cx, ty: cy});
    });
  });
  return spots;
}

function resize(){
  const w = Math.max(340, cv.parentElement.clientWidth);
  cv.width = w;
  const cols = Math.max(1, Math.floor((w - PAD * 2 + GAP_X) / (TABLE_W + GAP_X)));
  const rows = Math.ceil(SEATED.length / cols);
  cv.height = TOP + rows * (TABLE_H + GAP_Y) + 10;
  cv.style.width = '100%';
  cv.style.height = 'auto';
  draw();
}

function drawTable(cx, cy){
  const x = Math.round(cx - TABLE_W / 2), y = Math.round(cy - TABLE_H / 2);
  ctx.fillStyle = '#00000018';
  ctx.fillRect(x + 4, y + 8, TABLE_W, TABLE_H);
  ctx.fillStyle = '#9b7247';
  ctx.fillRect(x, y, TABLE_W, TABLE_H);
  ctx.fillStyle = '#b1865a';
  ctx.fillRect(x + 4, y + 4, TABLE_W - 8, TABLE_H - 8);
  // Доски столешницы — те же пиксели, просто темнее.
  ctx.fillStyle = '#a07a4e';
  for (let i = y + 12; i < y + TABLE_H - 8; i += 16) ctx.fillRect(x + 8, i, TABLE_W - 16, 2);
  // Чашки: по одной на стол, чтобы зал выглядел обжитым.
  ctx.fillStyle = '#efeae2';
  ctx.fillRect(x + 26, y + 30, 10, 8);
  ctx.fillRect(x + TABLE_W - 40, y + TABLE_H - 42, 10, 8);
}

function drawPerson(x, y, agent, active, back, bob){
  const p = palette(agent);
  const colors = {1: p.hair, 2: back ? p.hair : p.skin, 3: p.wear, 4: '#00000055'};
  const w = SPRITE[0].length * SCALE, h = SPRITE.length * SCALE;
  const left = Math.round(x - w / 2), top = Math.round(y - h + bob);

  if (active){
    ctx.fillStyle = '#f2c14e30';
    ctx.fillRect(left - 7, top - 7, w + 14, h + 14);
  }
  ctx.imageSmoothingEnabled = false;
  SPRITE.forEach((row, ry) => {
    [...row].forEach((ch, rx) => {
      if (ch === '0') return;
      ctx.fillStyle = colors[ch] || p.wear;
      ctx.fillRect(left + rx * SCALE, top + ry * SCALE, SCALE, SCALE);
    });
  });

  ctx.strokeStyle = EDGE[agent.stance] || EDGE.neutral;
  ctx.lineWidth = 2;
  ctx.strokeRect(left - 4.5, top - 4.5, w + 9, h + 9);

  // Имя: у дальних над головой, у ближних под ногами — иначе подписи
  // столкнутся со столешницей.
  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center';
  // У дальнего ряда места меньше: три подписи в ширину стола.
  const limit = back ? 11 : 14;
  const name = agent.name.length > limit ? agent.name.slice(0, limit - 1) + '…' : agent.name;
  ctx.fillText(name, x, back ? top - 10 : y + 16 + bob);
}

function speechMark(x, y, bob, back){
  // У дальнего ряда над головой стоит подпись — значок уводим на уровень плеч.
  const top = y - SPRITE.length * SCALE + (back ? 18 : -14) + bob;
  ctx.fillStyle = '#f2c14e';
  ctx.beginPath(); ctx.roundRect(x + 13, top - 6, 18, 13, 3); ctx.fill();
  ctx.beginPath();
  ctx.moveTo(x + 17, top + 7); ctx.lineTo(x + 23, top + 7); ctx.lineTo(x + 17, top + 12);
  ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#2a2a26';
  [0, 5, 10].forEach(dx => ctx.fillRect(x + 16 + dx, top, 2, 2));
}

function wrap(text, perLine, maxLines){
  const words = text.replace(/\s+/g, ' ').trim().split(' ');
  const lines = [];
  let line = '';
  for (const word of words){
    if ((line + ' ' + word).length > perLine){ lines.push(line); line = word; }
    else line = line ? line + ' ' + word : word;
    if (lines.length >= maxLines) break;
  }
  if (line && lines.length < maxLines) lines.push(line);
  if (lines.length === maxLines) lines[maxLines - 1] += '…';
  return lines;
}

function bubble(x, y, text, bob, lane){
  const lines = wrap(text, 26, 3);
  if (!lines.length) return;
  ctx.font = '11px system-ui, sans-serif';
  const w = 176, h = 14 * lines.length + 14;
  const bx = Math.min(Math.max(x - w / 2, 4), cv.width - w - 4);
  // 68 — нижняя граница двух строк заголовка сцены.
  // lane разводит пузыри по высоте: рядом стоящие иначе накрывают друг друга.
  const by = Math.max(68, y - SPRITE.length * SCALE - 30 - h + bob) - (lane || 0) * (h + 8);

  ctx.fillStyle = getComputedStyle(cv).backgroundColor;
  ctx.strokeStyle = EDGE.neutral;
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.roundRect(bx, by, w, h, 6); ctx.fill(); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x - 5, by + h); ctx.lineTo(x + 5, by + h); ctx.lineTo(x, by + h + 7);
  ctx.closePath(); ctx.fill();

  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.textAlign = 'left';
  lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 18 + i * 14));
}

function draw(){
  ctx.clearRect(0, 0, cv.width, cv.height);
  const acts = DATA.rounds[round] || [];
  const speaking = new Map();
  acts.forEach(a => { if (!speaking.has(a.agent)) speaking.set(a.agent, a); });

  ctx.fillStyle = getComputedStyle(document.body).color;
  ctx.font = '600 15px system-ui, sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText(`Раунд ${round + 1} из ${DATA.rounds.length} · говорят ${speaking.size} из ${DATA.agents.length}`,
               PAD, 32);
  ctx.font = '12px system-ui, sans-serif';
  ctx.fillStyle = getComputedStyle(document.querySelector('.hint')).color;
  ctx.fillText('Столы собраны по позиции: сторонники, нейтральные, скептики',
               PAD, 52);

  const spots = layout();
  // Порядок важен: дальние люди → столы (прячут их по пояс) → ближние люди.
  const far = spots.filter(s => s.back);
  const near = spots.filter(s => !s.back);

  far.forEach(s => {
    const active = speaking.has(s.agent.id);
    const bob = active ? Math.round(Math.sin(tick / 6 + s.agent.id) * 2) - 2 : 0;
    drawPerson(s.x, s.y, s.agent, active, true, bob);
  });

  const seen = new Set();
  spots.forEach(s => { if (!seen.has(s.table)){ seen.add(s.table); drawTable(s.tx, s.ty); } });

  // Спор за столом: тонкая нить между теми, кто говорит в одном раунде рядом.
  const byTable = new Map();
  spots.forEach(s => {
    if (!speaking.has(s.agent.id)) return;
    if (!byTable.has(s.table)) byTable.set(s.table, []);
    byTable.get(s.table).push(s);
  });
  ctx.setLineDash([3, 4]);
  ctx.strokeStyle = '#f2c14e99';
  ctx.lineWidth = 1.5;
  byTable.forEach(group => {
    for (let i = 0; i < group.length - 1; i++){
      ctx.beginPath();
      ctx.moveTo(group[i].x, group[i].y - 16);
      ctx.lineTo(group[i + 1].x, group[i + 1].y - 16);
      ctx.stroke();
    }
  });
  ctx.setLineDash([]);

  near.forEach(s => {
    const active = speaking.has(s.agent.id);
    // Говорящий слегка приподнимается — зал оживает, данные не искажаются.
    const bob = active ? Math.round(Math.sin(tick / 6 + s.agent.id) * 2) - 2 : 0;
    drawPerson(s.x, s.y, s.agent, active, false, bob);
  });

  spots.forEach(s => {
    if (!speaking.has(s.agent.id)) return;
    const bob = Math.round(Math.sin(tick / 6 + s.agent.id) * 2) - 2;
    speechMark(s.x, s.y, bob, s.back);
  });

  // Пузыри — поверх всех. Больше двух одновременно читать невозможно,
  // поэтому показываем две самые свежие реплики, остальное в ленте ниже.
  const talkers = spots.filter(s => speaking.has(s.agent.id));
  const focus = picked !== null
    ? talkers.filter(s => s.agent.id === picked)
    : talkers.slice(0, 2);
  let lastX = null;
  focus.forEach((s, i) => {
    // Второй пузырь поднимаем только если он реально перекрыл бы первый.
    const lane = (i > 0 && lastX !== null && Math.abs(s.x - lastX) < 186) ? 1 : 0;
    bubble(s.x, s.y, speaking.get(s.agent.id).text, -2, lane);
    lastX = s.x;
  });

  renderSaid(acts);
}

function renderSaid(acts){
  if (!acts.length){
    said.innerHTML = '<p class="hint">В этом раунде никто не заговорил.</p>';
    return;
  }
  const shown = picked === null ? acts : acts.filter(a => a.agent === picked);
  said.innerHTML = shown.map(a => {
    const agent = DATA.agents.find(x => x.id === a.agent) || {name: 'Агент'};
    return `<div class="line"><span class="who">${esc(agent.name)}</span>` +
           `<span class="plat">${esc(a.platform)} · ${esc(a.type)}</span><br>${esc(a.text)}</div>`;
  }).join('') || '<p class="hint">Этот участник в раунде промолчал.</p>';
}

function esc(s){
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function animate(){
  tick++;
  draw();
  raf = requestAnimationFrame(() => setTimeout(animate, 120));
}

cv.addEventListener('click', ev => {
  const rect = cv.getBoundingClientRect();
  const x = (ev.clientX - rect.left) * (cv.width / rect.width);
  const y = (ev.clientY - rect.top) * (cv.height / rect.height);
  const hit = layout().find(s => Math.abs(s.x - x) < 26 && y > s.y - 58 && y < s.y + 16);
  picked = hit && picked !== hit.agent.id ? hit.agent.id : null;
  label.textContent = picked === null ? ''
    : (DATA.agents.find(a => a.id === picked)?.bio || 'описание не сохранилось');
  draw();
});

slider.addEventListener('input', () => { round = +slider.value; draw(); });
playBtn.addEventListener('click', () => {
  playing = !playing;
  playBtn.textContent = playing ? '❚❚ Пауза' : '▶ Проиграть';
  clearInterval(timer);
  if (playing){
    timer = setInterval(() => {
      round = (round + 1) % DATA.rounds.length;
      slider.value = round;
      draw();
      if (round === DATA.rounds.length - 1){
        playing = false; playBtn.textContent = '▶ Проиграть'; clearInterval(timer);
      }
    }, 2600);
  }
});

window.addEventListener('beforeprint', () => {
  if (raf) cancelAnimationFrame(raf);
  const all = DATA.rounds.map((acts, i) => {
    if (!acts.length) return `<div class="line"><i>Раунд ${i + 1}: никто не заговорил.</i></div>`;
    return `<h3>Раунд ${i + 1}</h3>` + acts.map(a => {
      const agent = DATA.agents.find(x => x.id === a.agent) || {name: 'Агент'};
      return `<div class="line"><span class="who">${esc(agent.name)}</span>` +
             `<span class="plat">${esc(a.platform)} · ${esc(a.type)}</span><br>${esc(a.text)}</div>`;
    }).join('');
  }).join('');
  said.innerHTML = all;
});
window.addEventListener('afterprint', draw);
window.addEventListener('resize', resize);

resize();
animate();
"""


def render_scene(scene: Dict[str, Any], title: str) -> str:
    if scene.get("error") or not scene["agents"]:
        return (f'<div class="card"><h2>Сцена</h2><p class="hint">'
                f'{html.escape(scene.get("error") or "нет действий для показа")}</p></div>')

    payload = json.dumps(
        {"agents": scene["agents"], "rounds": scene["rounds"]},
        ensure_ascii=False,
    ).replace("</", "<\\/")

    rounds_count = len(scene["rounds"])
    total_acts = sum(len(r) for r in scene["rounds"])
    agents = scene["agents"]
    rows = stance_breakdown(agents)
    spoke = sum(1 for a in agents if a["acts"])

    tally_rows = "".join(
        f'<div class="row">{html.escape(r["title"])}<b>{r["percent"]}%</b>'
        f'<div class="track"><i style="width:{r["percent"]}%;'
        f'background:{r["color"]}"></i></div></div>'
        for r in rows)

    return f"""
<style>{SCENE_CSS}</style>
<div class="card scene-wrap">
  <h2>Зал: кто что говорил</h2>
  <p class="hint">{len(agents)} участников · {rounds_count} раундов ·
  {total_acts} реплик. Соседи по столу — люди близкой позиции; жёлтая нить
  связывает тех, кто говорит за одним столом в этом раунде.
  Нажми на человека — покажу, кто это и что он говорил.</p>

  <div class="stage-row">
    <div class="stage-col"><canvas id="stage"></canvas></div>
    <div class="tally">
      <h3>Расклад зала</h3>
      <div class="ring">{donut_svg(rows, len(agents))}</div>
      {tally_rows}
      <p class="foot">Высказались {spoke} из {len(agents)}.<br>
      Позиция взята из описания персоны, которое составил сам движок,
      а не из её реплик.</p>
    </div>
  </div>

  <div class="controls no-print">
    <button id="play" type="button">▶ Проиграть</button>
    <input type="range" id="slider" min="0" max="{max(0, rounds_count - 1)}" value="0">
  </div>
  <div class="legend">
    <span><i class="dot" style="background:#2f8f5f"></i>скорее за</span>
    <span><i class="dot" style="background:#8a8a84"></i>нейтрален</span>
    <span><i class="dot" style="background:#b4493a"></i>скорее против</span>
    <span><i class="dot" style="background:#f2c14e"></i>говорит в этом раунде</span>
  </div>
  <p class="hint" id="label"></p>
  <div id="said"></div>
</div>
<script>{SCENE_JS.replace("__DATA__", payload)}</script>
"""
