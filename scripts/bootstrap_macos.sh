#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

START_DASHBOARD=0
INSTALL_VSCODE=1

for arg in "$@"; do
  case "$arg" in
    --start-dashboard) START_DASHBOARD=1 ;;
    --no-vscode) INSTALL_VSCODE=0 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: ./scripts/bootstrap_macos.sh [--start-dashboard] [--no-vscode]" >&2
      exit 2
      ;;
  esac
done

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

echo "Project: $ROOT_DIR"

if ! have_cmd python3; then
  echo "Python 3 was not found. Installing Python with Homebrew..."
  if ! have_cmd brew; then
    echo "Homebrew was not found. Installing Homebrew first..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [[ -x /opt/homebrew/bin/brew ]]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi
  brew install python
fi

PYTHON_BIN="$(command -v python3)"
echo "Using Python: $($PYTHON_BIN --version) at $PYTHON_BIN"

if [[ "$INSTALL_VSCODE" == "1" ]] && ! have_cmd code; then
  if have_cmd brew; then
    echo "VS Code command 'code' was not found. Installing Visual Studio Code..."
    brew install --cask visual-studio-code || echo "VS Code install failed or was skipped. The app can still run without VS Code."
  else
    echo "VS Code command 'code' was not found. Install VS Code manually if you want an editor."
  fi
fi

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment: .venv"
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
if [[ -s requirements.txt ]]; then
  python -m pip install -r requirements.txt
fi

if [[ ! -f .env ]]; then
  echo "Creating .env from .env.example. Fill API keys and SMTP settings before using paid APIs or real sending."
  cp .env.example .env
else
  echo ".env already exists. Keeping it unchanged."
fi

mkdir -p data
python -m compileall trade_outreach
python -m trade_outreach.cli init-db

echo
echo "Bootstrap complete."
echo "Activate later with: source .venv/bin/activate"
echo "Start dashboard with: python -m trade_outreach.dashboard"
echo "Open: http://127.0.0.1:8765"

if [[ "$START_DASHBOARD" == "1" ]]; then
  python -m trade_outreach.dashboard
fi
