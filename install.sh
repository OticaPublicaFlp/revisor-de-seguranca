#!/usr/bin/env bash
# Instala o revisor de segurança: ambiente Python isolado (.venv), dependências e o atalho `check`.
# Uso: ./install.sh        (pode rodar de novo a qualquer momento para atualizar)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

step() { printf '\n\033[1;36m▶\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

has git || die "O git não está instalado. No Ubuntu/Debian: sudo apt install git"

step "Criando o ambiente Python (.venv)"
if has uv; then
  uv venv -q --allow-existing --python ">=3.11" .venv
  uv pip install -q -p .venv/bin/python -e .
else
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if has "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      PY="$candidate"; break
    fi
  done
  [[ -n "$PY" ]] || die "É preciso Python 3.11 ou mais novo. Alternativa simples: instale o uv
   (curl -LsSf https://astral.sh/uv/install.sh | sh) e rode ./install.sh de novo."
  "$PY" -m venv .venv 2>/dev/null || die "Não consegui criar o ambiente Python.
   No Ubuntu/Debian: sudo apt install python3-venv   (ou instale o uv e rode de novo)"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -e .
fi
echo "  ✓ dependências instaladas"

step "Instalando o atalho 'check' em ~/.local/bin"
mkdir -p "$HOME/.local/bin"
chmod +x scripts/check install.sh
TARGET="$HOME/.local/bin/check"
if [[ -e "$TARGET" && "$(readlink -f "$TARGET")" != "$DIR/scripts/check" ]]; then
  warn "Já existe outro comando 'check' em $TARGET. Não vou sobrescrever."
  echo "  Use direto: $DIR/scripts/check   (ou apague o outro e rode ./install.sh de novo)"
else
  ln -sf "$DIR/scripts/check" "$TARGET"
  echo "  ✓ atalho instalado"
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "A pasta ~/.local/bin não está no PATH. Rode uma vez:"
     echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
esac

step "Pronto!"
echo "  Próximo passo: check configurar   (assistente que pergunta tudo e valida cada chave)"
if [[ -t 0 && ! -f .env ]]; then
  read -r -p "  Abrir o assistente agora? [S/n] " answer
  if [[ -z "$answer" || "$answer" =~ ^[sSyY] ]]; then
    exec "$DIR/scripts/check" configurar
  fi
fi
