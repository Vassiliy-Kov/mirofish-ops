#!/usr/bin/env bash
# Выкат Telegram-бота MiroFish. Запускать ПОСЛЕ install-mirofish.sh и авторизации claude.
# Ожидает, что bot.py и mirofish-bot.service уже лежат в /tmp.
set -euo pipefail

APP_USER=mirofish
APP_HOME=/opt/mirofish
ENV_FILE="$APP_HOME/.env"
LOG=/var/log/mirofish-bot.log

echo "=== 1. Код бота ==="
sudo install -o "$APP_USER" -g "$APP_USER" -m 640 /tmp/bot.py "$APP_HOME/bot.py"
sudo -u "$APP_USER" mkdir -p "$APP_HOME/state/inbox"

echo "=== 2. Конфиг ==="
if [ ! -f "$ENV_FILE" ]; then
  sudo -u "$APP_USER" tee "$ENV_FILE" >/dev/null <<'ENV'
# Токен от @BotFather
TELEGRAM_BOT_TOKEN=ВСТАВЬ_СЮДА
# Числовые Telegram id (не @username), через запятую
TELEGRAM_ALLOWED_USERS=ВСТАВЬ_СЮДА
# Раундов симуляции. 2 ≈ 10 минут. Больше — дольше и точнее.
MIROFISH_MAX_ROUNDS=2
ENV
  sudo chmod 600 "$ENV_FILE"
  echo "Создан $ENV_FILE — впиши токен и id, иначе бот не стартует."
else
  echo "$ENV_FILE уже есть, не трогаю"
fi

echo "=== 3. Лог ==="
sudo touch "$LOG"
sudo chown "$APP_USER:$APP_USER" "$LOG"
sudo tee /etc/logrotate.d/mirofish-bot >/dev/null <<ROT
$LOG {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
ROT

echo "=== 4. systemd-юнит с лимитами ==="
sudo install -m 644 /tmp/mirofish-bot.service /etc/systemd/system/mirofish-bot.service
sudo systemctl daemon-reload

if grep -q 'ВСТАВЬ_СЮДА' "$ENV_FILE"; then
  echo
  echo "Юнит установлен, но НЕ запущен: в $ENV_FILE остались плейсхолдеры."
  echo "Заполни их и выполни: sudo systemctl enable --now mirofish-bot"
  exit 0
fi

sudo systemctl enable --now mirofish-bot
sleep 3
systemctl --no-pager -l status mirofish-bot | head -20
echo
echo "Лимиты:"
systemctl show mirofish-bot -p MemoryMax -p CPUQuotaPerSecUSec -p RestartUSec -p StartLimitBurst
