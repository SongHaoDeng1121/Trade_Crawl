#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INCLUDE_ENV=0
for arg in "$@"; do
  case "$arg" in
    --include-env) INCLUDE_ENV=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: ./scripts/make_transfer_bundle.sh [--include-env]" >&2
      exit 2
      ;;
  esac
done

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="$ROOT_DIR/transfer"
OUT_FILE="$OUT_DIR/trade-crawl-transfer-$STAMP.tar.gz"
mkdir -p "$OUT_DIR"

EXCLUDES=(
  "--exclude=.git"
  "--exclude=.venv"
  "--exclude=__pycache__"
  "--exclude=*.pyc"
  "--exclude=.DS_Store"
  "--exclude=transfer"
)

if [[ "$INCLUDE_ENV" != "1" ]]; then
  EXCLUDES+=("--exclude=.env")
  echo "Creating transfer bundle without .env secrets."
  echo "Copy .env separately with a secure method if the new device should use the same API/SMTP keys."
else
  echo "Creating transfer bundle with .env included. Keep this archive private."
fi

tar -czf "$OUT_FILE" "${EXCLUDES[@]}" .

echo "Created: $OUT_FILE"
