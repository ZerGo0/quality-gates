#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly RUNTIME_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/quality-gates/llama.cpp"
readonly MODEL_CACHE="$RUNTIME_DIR/cache"
readonly SETUP_FILE="$RUNTIME_DIR/setup.txt"
readonly SERVER_LOG="$RUNTIME_DIR/setup-server.log"
readonly ADAPTER_LOG="$RUNTIME_DIR/setup-adapter.log"
readonly SERVER_HOST="127.0.0.1"
readonly SERVER_PORT="17997"
readonly ADAPTER_PORT="17998"
readonly MODEL_ALIAS="jina-code-embeddings-0.5b"
readonly MODEL_REPOSITORY="jinaai/jina-code-embeddings-0.5b-GGUF"
readonly MODEL_FILE="jina-code-embeddings-0.5b-Q8_0.gguf"
readonly SLOPO_PROJECT="$SCRIPT_DIR/../slopo"
readonly SLOPO_BIN="$SLOPO_PROJECT/.venv/bin/slopo"
readonly SLOPO_PYTHON="$SLOPO_PROJECT/.venv/bin/python"
readonly ADAPTER_SCRIPT="$SLOPO_PROJECT/local_jina_adapter.py"

server_pid=""
adapter_pid=""

cleanup() {
  if [[ -n "$adapter_pid" ]] && kill -0 "$adapter_pid" 2>/dev/null; then
    kill "$adapter_pid" 2>/dev/null || true
    wait "$adapter_pid" 2>/dev/null || true
  fi
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
}

die() {
  printf 'setup-slopo-llama-cpp: %s\n' "$*" >&2
  exit 1
}

wait_for_server() {
  for _ in {1..300}; do
    if ! kill -0 "$server_pid" 2>/dev/null; then
      tail -n 80 "$SERVER_LOG" >&2 || true
      die 'llama-server exited before becoming ready'
    fi
    if curl --fail --silent --show-error "http://${SERVER_HOST}:${SERVER_PORT}/health" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  tail -n 80 "$SERVER_LOG" >&2 || true
  die 'llama-server did not become ready within five minutes'
}

resolve_llama() {
  if command -v llama >/dev/null 2>&1; then
    command -v llama
  elif [[ -x "$HOME/.local/bin/llama" ]]; then
    printf '%s\n' "$HOME/.local/bin/llama"
  elif [[ -x "$HOME/.llama-app/llama" ]]; then
    printf '%s\n' "$HOME/.llama-app/llama"
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) die "unsupported operating system: $(uname -s); llama.cpp setup supports macOS and Linux here" ;;
esac

command -v curl >/dev/null 2>&1 || die 'curl is required by the official llama.cpp installer'
command -v uv >/dev/null 2>&1 || die 'uv is required to install the locked Slopo environment'

llama_bin="$(resolve_llama || true)"
if [[ -z "$llama_bin" ]]; then
  curl -LsSf https://llama.app/install.sh | sh
  hash -r
  llama_bin="$(resolve_llama || true)"
fi
[[ -n "$llama_bin" ]] || die 'the official installer completed but the llama.cpp CLI could not be found'

uv sync --frozen --no-dev --project "$SLOPO_PROJECT"
[[ -x "$SLOPO_BIN" && -x "$SLOPO_PYTHON" ]] || die 'the locked Slopo environment is incomplete'

mkdir -p "$MODEL_CACHE"
export LLAMA_CACHE="$MODEL_CACHE"
"$llama_bin" serve \
  --embedding \
  --hf-repo "$MODEL_REPOSITORY" \
  --hf-file "$MODEL_FILE" \
  --alias "$MODEL_ALIAS" \
  --host "$SERVER_HOST" \
  --port "$SERVER_PORT" \
  --ctx-size 8192 \
  --ubatch-size 8192 \
  --pooling last \
  --embd-normalize 2 \
  --cache-ram 0 \
  --no-webui \
  >"$SERVER_LOG" 2>&1 &
server_pid=$!
wait_for_server

"$SLOPO_PYTHON" "$ADAPTER_SCRIPT" \
  --host "$SERVER_HOST" \
  --port "$ADAPTER_PORT" \
  --upstream-url "http://${SERVER_HOST}:${SERVER_PORT}/v1/embeddings" \
  >"$ADAPTER_LOG" 2>&1 &
adapter_pid=$!
for _ in {1..30}; do
  if ! kill -0 "$adapter_pid" 2>/dev/null; then
    tail -n 80 "$ADAPTER_LOG" >&2 || true
    die 'local Jina adapter exited before becoming ready'
  fi
  if curl --fail --silent --show-error "http://${SERVER_HOST}:${ADAPTER_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
kill -0 "$adapter_pid" 2>/dev/null || die 'local Jina adapter exited before becoming ready'
curl --fail --silent --show-error "http://${SERVER_HOST}:${ADAPTER_PORT}/health" >/dev/null 2>&1 || die 'local Jina adapter did not become ready within 30 seconds'

export LITELLM_LOCAL_MODEL_COST_MAP=True
export DO_NOT_TRACK=true
export SCARF_NO_ANALYTICS=true
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
"$SLOPO_PYTHON" - "$SERVER_HOST" "$ADAPTER_PORT" <<'PY'
import math
import sys

import litellm
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

litellm.suppress_debug_info = True
cost_map_source = get_model_cost_map_source_info()
if cost_map_source["source"] != "local" or not cost_map_source["is_env_forced"]:
    raise SystemExit("LiteLLM did not load its bundled model cost map")
response = litellm.embedding(
    model="jina_ai/jina-code-embeddings-0.5b",
    input=["validation"],
    dimensions=256,
    api_key="local",
    api_base=f"http://{sys.argv[1]}:{sys.argv[2]}/v1",
    task="code2code.query",
)
embedding = response.data[0]["embedding"]
if len(embedding) != 256 or not all(
    isinstance(value, (int, float))
    and not isinstance(value, bool)
    and math.isfinite(value)
    for value in embedding
):
    raise SystemExit("LiteLLM did not receive a valid local 256-dimension embedding")
norm = math.sqrt(sum(value * value for value in embedding))
if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
    raise SystemExit("local Jina adapter did not normalize the truncated embedding")
PY

setup_file="$(mktemp "$RUNTIME_DIR/.setup.XXXXXX")"
printf 'model_repository=%s\nmodel_file=%s\nembedding_dimensions=256\npooling=last\ntask=code2code.query\nadapter=local-jina-api\nprompt_cache=disabled\n' \
  "$MODEL_REPOSITORY" "$MODEL_FILE" >"$setup_file"
mv "$setup_file" "$SETUP_FILE"

printf 'Slopo is ready with llama.cpp and %s.\n' "$MODEL_FILE"
