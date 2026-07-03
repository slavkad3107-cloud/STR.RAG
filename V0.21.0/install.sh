#!/usr/bin/env bash
# STROYPROEKT — установка (Linux/macOS), устойчивая к медленной сети.
set -e
echo "=== STROYPROEKT — установка ==="
command -v python3 >/dev/null 2>&1 || { echo "Python 3 не найден"; exit 1; }
export PIP_DEFAULT_TIMEOUT=120

DATA="${PMOOS_DATA_DIR:-$HOME/.pmoos-rag}"
mkdir -p "$DATA"
export PIP_CACHE_DIR="$DATA/pip-cache"
VENV="$DATA/venv"
echo "[env] общий venv в папке данных: $VENV"
echo "[env] новые версии приложения переиспользуют его — повторно ничего не качается"
if [ ! -f "$VENV/bin/activate" ]; then python3 -m venv "$VENV"; fi
source "$VENV/bin/activate"
python -m pip install --upgrade pip --timeout 120 --retries 10

echo "[torch] CUDA 12.4 (для CPU/Mac будет обычная сборка)..."
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 --timeout 180 --retries 10 \
  || pip install torch==2.6.0 --timeout 180 --retries 10

echo "[deps] попытка 1: индекс по умолчанию (fail-fast)..."
if ! pip install --prefer-binary --timeout 60 --retries 2 -r requirements.txt; then
  echo "[deps] индекс по умолчанию нестабилен — переключаюсь на зеркало..."
  export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
  export PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
  if ! pip install --prefer-binary --timeout 120 --retries 10 -r requirements.txt; then
    echo "[deps] ставлю по одному пакету (успешные сохранятся)..."
    grep -vE '^\s*#' requirements.txt | grep -vE '^\s*$' | awk '{print $1}' | while read -r pkg; do
      echo "   -> $pkg"
      pip install --prefer-binary --timeout 120 --retries 10 "$pkg" || true
    done
  fi
fi

cp -f .env.example "$DATA/.env.example"
if [ ! -f "$DATA/.env" ]; then
  if [ -f .env ]; then cp .env "$DATA/.env"; echo "[keys] перенесён старый .env из папки приложения";
  else cp .env.example "$DATA/.env"; fi
fi
echo "[keys] файл ключей: $DATA/.env"

echo "[models] скачивание моделей ИИ (bge-m3 + reranker, ~3.4 ГБ) и самопроверка..."
python setup_models.py || echo "[models] ВНИМАНИЕ: модели не скачались/не прошли проверку — повторите позже: python setup_models.py"

python -c "import numpy; print('NumPy:', numpy.__version__)"
python -c "import torch; print('CUDA:', torch.cuda.is_available())"
echo "Готово. Запуск: ./run.sh  (если что-то не докачалось — запустите снова)"
