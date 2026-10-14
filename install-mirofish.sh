#!/usr/bin/env bash
# Установка MiroFish (форк amadad/mirofish) на mozgi VPS.
# Изолированно: отдельный системный пользователь, свой Node и Python, ничего
# системного не трогаем, боевой mozgi не касаемся. Идемпотентен — можно гонять повторно.
set -euo pipefail

APP_USER=mirofish
APP_HOME=/opt/mirofish
APP_DIR="$APP_HOME/app"
REPO=https://github.com/amadad/mirofish
NODE_VER=v22.11.0
PY_VER=3.12

say() { printf '\n=== %s ===\n' "$1"; }

say "0. Гейт по диску (урок инцидента на проде)"
AVAIL_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
echo "Свободно на /: ${AVAIL_GB} ГБ"
if [ "$AVAIL_GB" -lt 10 ]; then
  echo "СТОП: меньше 10 ГБ свободно. Сначала чистим диск." >&2
  exit 1
fi

say "1. Системный пользователь $APP_USER и каталог $APP_HOME"
if ! id "$APP_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir "$APP_HOME" --shell /bin/bash "$APP_USER"
fi
sudo chown "$APP_USER:$APP_USER" "$APP_HOME"
sudo chmod 750 "$APP_HOME"
id "$APP_USER"

say "2. Системные зависимости сборки (без Node и Python из apt)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates build-essential >/dev/null
echo "ok"

# Дальше всё — от имени mirofish, в его домашнем каталоге.
sudo -u "$APP_USER" -H bash -euo pipefail <<EOF
APP_HOME="$APP_HOME"; APP_DIR="$APP_DIR"; REPO="$REPO"; NODE_VER="$NODE_VER"; PY_VER="$PY_VER"
export PATH="\$APP_HOME/.local/bin:\$APP_HOME/node/bin:\$APP_HOME/bin:\$PATH"
# sudo -u не меняет рабочий каталог: без cd мы остаёмся в /home/ubuntu, куда у
# mirofish нет прав, и uv падает на попытке прочитать там uv.toml.
cd "\$APP_HOME"
mkdir -p "\$APP_HOME/bin"

echo "=== 3. uv ==="
if [ ! -x "\$APP_HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="\$APP_HOME/.local/bin" sh
fi
"\$APP_HOME/.local/bin/uv" --version

echo "=== 4. Python \$PY_VER через uv ==="
"\$APP_HOME/.local/bin/uv" python install "\$PY_VER"

echo "=== 5. Node \$NODE_VER локально (для claude CLI) ==="
if [ ! -x "\$APP_HOME/node/bin/node" ]; then
  curl -fsSL "https://nodejs.org/dist/\$NODE_VER/node-\$NODE_VER-linux-x64.tar.xz" -o /tmp/node.tar.xz
  mkdir -p "\$APP_HOME/node"
  tar -xJf /tmp/node.tar.xz -C "\$APP_HOME/node" --strip-components=1
  rm -f /tmp/node.tar.xz
fi
node --version
npm --version

echo "=== 6. claude CLI (подписка, не API) ==="
if ! command -v claude >/dev/null 2>&1; then
  npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
fi
claude --version || true
# Шим: mirofish doctor ищет бинарь с именем claude-cli
ln -sf "\$(command -v claude)" "\$APP_HOME/bin/claude-cli"
ls -l "\$APP_HOME/bin/claude-cli"

echo "=== 7. Форк mirofish ==="
if [ ! -d "\$APP_DIR/.git" ]; then
  git clone --depth 1 "\$REPO" "\$APP_DIR"
else
  git -C "\$APP_DIR" pull --ff-only
fi

echo "=== 8. venv + установка пакета ==="
cd "\$APP_DIR"
# uv sync НЕ используем намеренно: в uv.lock приколочен rapidfuzz, из-за которого
# ставили грабли на Windows. uv pip install -e . даёт резолверу свободу.
"\$APP_HOME/.local/bin/uv" venv --python "\$PY_VER" .venv
"\$APP_HOME/.local/bin/uv" pip install --python .venv/bin/python -e . >/dev/null
.venv/bin/mirofish --help >/dev/null && echo "mirofish CLI: ok"

echo "=== 9. PATH в профиле пользователя ==="
grep -q 'MIROFISH PATH' "\$APP_HOME/.bashrc" 2>/dev/null || cat >> "\$APP_HOME/.bashrc" <<'RC'
# MIROFISH PATH
export PATH="/opt/mirofish/.local/bin:/opt/mirofish/node/bin:/opt/mirofish/bin:$PATH"
export LLM_PROVIDER=claude-cli
RC
echo "ok"
EOF

say "ГОТОВО — установка"
echo "Дальше нужен один интерактивный шаг (авторизация подписки):"
echo "  sudo -u $APP_USER -H bash -lc 'claude'   # пройти вход, вставить код"
echo "Потом: sudo -u $APP_USER -H bash -lc 'cd $APP_DIR && .venv/bin/mirofish doctor'"
