#!/usr/bin/env bash
# Выкат веб-интерфейса MiroFish на <домен>/ai за basic-auth.
# Запускать ПОСЛЕ install-mirofish.sh и авторизации claude.
# Ожидает web.py и mirofish-web.service в /tmp. Идемпотентен.
#
# Боевой nginx правится с бэкапом и проверкой `nginx -t`; при ошибке — откат.
set -euo pipefail

APP_USER=mirofish
APP_HOME=/opt/mirofish
ENV_FILE="$APP_HOME/.env"
LOG=/var/log/mirofish-web.log
SNIPPET=/etc/nginx/snippets/mirofish.conf
HTPASSWD=/etc/nginx/.mirofish-htpasswd
WEB_LOGIN="${WEB_LOGIN:-admin}"
PORT=8777
# Домен сайта, в nginx-конфиг которого встраиваемся. Переопределяется
# переменной окружения: SITE_DOMAIN=example.com bash deploy-web.sh
SITE_DOMAIN="${SITE_DOMAIN:-mozgi.kz}"

say() { printf '\n=== %s ===\n' "$1"; }

say "0. Идёт ли сейчас прогон"
# Рестарт сервиса убивает дочерний процесс симуляции вместе с задачей.
# Один раз так уже оборвали чужой прогон на середине — больше не повторяем.
#
# Смотрим журнал задач, а не процессы: движок после отчёта остаётся висеть
# в режиме ожидания команд, и проверка по pgrep блокировала бы выкат навсегда.
BUSY=$(sudo python3 - <<'PY'
import json, pathlib
path = pathlib.Path("/opt/mirofish/state/jobs.json")
try:
    jobs = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print(0)
else:
    print(sum(1 for j in jobs if j.get("state") in ("running", "queued")))
PY
)
if [ "${BUSY:-0}" != "0" ]; then
  if [ "${FORCE_DEPLOY:-0}" = "1" ]; then
    echo "ВНИМАНИЕ: в работе $BUSY задач(и), но FORCE_DEPLOY=1 — обрываю."
  else
    echo "СТОП: в работе $BUSY задач(и), выкат оборвёт их." >&2
    echo "Дождись окончания или запусти с FORCE_DEPLOY=1." >&2
    exit 2
  fi
else
  echo "свободно"
fi

say "1. Код интерфейса"
sudo install -o "$APP_USER" -g "$APP_USER" -m 640 /tmp/web.py "$APP_HOME/web.py"
sudo install -o "$APP_USER" -g "$APP_USER" -m 640 /tmp/ingest.py "$APP_HOME/ingest.py"
sudo install -o "$APP_USER" -g "$APP_USER" -m 640 /tmp/scene.py "$APP_HOME/scene.py"
sudo install -o "$APP_USER" -g "$APP_USER" -m 640 /tmp/translate.py "$APP_HOME/translate.py"
for png in logo.png favicon.png; do
  [ -f "/tmp/assets/$png" ] && sudo install -o "$APP_USER" -g "$APP_USER" -m 644 -D \
    "/tmp/assets/$png" "$APP_HOME/assets/$png"
done
sudo -u "$APP_USER" mkdir -p "$APP_HOME/state/inbox"
# Сервис уже может работать — подхватываем новый код.
systemctl is-active --quiet mirofish-web && sudo systemctl restart mirofish-web || true

say "2. Конфиг"
if [ ! -f "$ENV_FILE" ]; then
  sudo -u "$APP_USER" touch "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
fi
grep -q MIROFISH_WEB_PORT "$ENV_FILE" 2>/dev/null || \
  echo "MIROFISH_WEB_PORT=$PORT" | sudo tee -a "$ENV_FILE" >/dev/null
grep -q MIROFISH_MAX_ROUNDS "$ENV_FILE" 2>/dev/null || \
  echo "MIROFISH_MAX_ROUNDS=2" | sudo tee -a "$ENV_FILE" >/dev/null
sudo chown "$APP_USER:$APP_USER" "$ENV_FILE"

# Движок читает свой .env рядом с кодом — без него doctor валит проверку провайдера.
APP_ENV="$APP_HOME/app/.env"
if ! sudo test -f "$APP_ENV"; then
  printf 'LLM_PROVIDER=claude-cli\n' | sudo -u "$APP_USER" tee "$APP_ENV" >/dev/null
fi
sudo grep -q '^LLM_PROVIDER=' "$APP_ENV" || \
  printf 'LLM_PROVIDER=claude-cli\n' | sudo -u "$APP_USER" tee -a "$APP_ENV" >/dev/null
echo "движок: $(sudo cat "$APP_ENV" | tr '\n' ' ')"

say "2b. Ключ OpenRouter (пока может быть пустым)"
if ! sudo grep -q '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null; then
  printf '# Ключ с openrouter.ai — без него доступен только режим подписки.\nOPENROUTER_API_KEY=\n' \
    | sudo tee -a "$ENV_FILE" >/dev/null
fi
if sudo grep -q '^OPENROUTER_API_KEY=.\+' "$ENV_FILE" 2>/dev/null; then
  echo "ключ OpenRouter: задан, платные режимы доступны"
else
  echo "ключ OpenRouter: пуст — работает только подписка (это штатно)"
fi

say "2c. Врезки OpenRouter в движок"
if [ -d /tmp/patches ]; then
  for f in openrouter_model.py audience_expander.py apply.py; do
    sudo install -o "$APP_USER" -g "$APP_USER" -m 644 -D "/tmp/patches/$f" \
      "$APP_HOME/patches/$f"
  done
  sudo -u "$APP_USER" "$APP_HOME/app/.venv/bin/python" "$APP_HOME/patches/apply.py" "$APP_HOME/app"
else
  echo "каталог /tmp/patches не найден — врезки пропущены"
fi

say "3. Лог и ротация"
sudo touch "$LOG"
sudo chown "$APP_USER:$APP_USER" "$LOG"
sudo tee /etc/logrotate.d/mirofish-web >/dev/null <<ROT
$LOG {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
ROT

say "4. systemd-юнит с лимитами"
sudo install -m 644 /tmp/mirofish-web.service /etc/systemd/system/mirofish-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now mirofish-web
sleep 2
systemctl is-active mirofish-web

say "5. Пароль на вход"
if ! command -v htpasswd >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apache2-utils >/dev/null
fi
if ! sudo test -f "$HTPASSWD"; then
  # Не `tr </dev/urandom | head`: head закрывает поток, tr получает SIGPIPE,
  # а pipefail + set -e роняют на этом весь скрипт.
  WEB_PASS="$(openssl rand -hex 8)"
  sudo htpasswd -bc "$HTPASSWD" "$WEB_LOGIN" "$WEB_PASS" >/dev/null 2>&1
  sudo chown root:www-data "$HTPASSWD"
  sudo chmod 640 "$HTPASSWD"
  echo "СОЗДАН ВХОД — запиши, второй раз не покажу:"
  echo "   логин:  $WEB_LOGIN"
  echo "   пароль: $WEB_PASS"
else
  echo "$HTPASSWD уже есть, пароль не трогаю"
fi

say "6. nginx: сниппет"
sudo tee "$SNIPPET" >/dev/null <<SNIP
# MiroFish — внутренний инструмент, наружу только за паролем.
location /ai/ {
    auth_basic "MiroFish";
    auth_basic_user_file $HTPASSWD;

    proxy_pass http://127.0.0.1:$PORT;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # Прогон идёт десятки минут, но страница отвечает сразу —
    # запас на случай тяжёлого отчёта.
    proxy_read_timeout 300s;
    client_max_body_size 6m;
}
location = /ai { return 301 /ai/; }
SNIP
sudo nginx -t

say "7. nginx: включение сниппета в конфиг $SITE_DOMAIN"
# -R, а не -r: в sites-enabled лежат симлинки, по которым -r не ходит.
# Якорь на пробел перед именем, иначе regex поймает ещё и поддомены.
CONF=$(sudo grep -RlE "server_name[[:space:]]+${SITE_DOMAIN//./\\.}" \
  /etc/nginx/sites-enabled/ 2>/dev/null | head -1 || true)
[ -n "$CONF" ] && CONF=$(readlink -f "$CONF")
if [ -z "$CONF" ]; then
  echo "НЕ НАШЁЛ конфиг с server_name $SITE_DOMAIN — включи сниппет вручную:" >&2
  echo "   include $SNIPPET;   # внутрь server-блока с listen 443" >&2
  exit 1
fi
echo "конфиг: $CONF"

if sudo grep -q 'snippets/mirofish.conf' "$CONF"; then
  echo "include уже на месте"
else
  BACKUP="$HOME/backups/nginx-$SITE_DOMAIN-$(date +%Y%m%d-%H%M%S).conf"
  sudo mkdir -p "$HOME/backups"
  sudo cp "$CONF" "$BACKUP"
  echo "бэкап: $BACKUP"

  # Вставка include в ssl-блок: ищем server-блок, где есть listen 443.
  sudo python3 - "$CONF" "$SNIPPET" "$SITE_DOMAIN" <<'PY'
import re, sys
path, snippet, domain = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()

blocks, depth, start = [], 0, None
for i, ch in enumerate(text):
    if ch == "{":
        if depth == 0:
            start = i
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0 and start is not None:
            blocks.append((start, i))
            start = None

target = None
for s, e in blocks:
    body = text[s:e]
    if re.search(r"listen\s+443", body) and re.search(
            r"server_name\s+" + re.escape(domain), body):
        target = (s, e)
if target is None:
    sys.exit(f"не нашёл server-блок с listen 443 и server_name {domain}")

s, e = target
patched = text[:e] + f"\n    include {snippet};\n" + text[e:]
open(path, "w", encoding="utf-8").write(patched)
print("include вставлен в блок listen 443")
PY

  if ! sudo nginx -t; then
    echo "nginx -t не прошёл — откатываю конфиг" >&2
    sudo cp "$BACKUP" "$CONF"
    sudo nginx -t
    exit 1
  fi
fi

sudo systemctl reload nginx
say "ГОТОВО"
echo "Интерфейс: https://$SITE_DOMAIN/ai/   (логин $WEB_LOGIN)"
echo "Проверка боевого сайта:"
curl -s -o /dev/null -w "$SITE_DOMAIN        %{http_code}\n" "https://$SITE_DOMAIN/"
curl -s -o /dev/null -w "$SITE_DOMAIN/ai (401 ожидаем)  %{http_code}\n" "https://$SITE_DOMAIN/ai/"
