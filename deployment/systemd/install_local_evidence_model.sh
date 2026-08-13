#!/usr/bin/env bash
set -euo pipefail
umask 027

ACTIVATE=${1:-}
BASE=/opt/finance-radar/evidence-llm
TAG=b10068
LLAMA_ARCHIVE="llama-${TAG}-bin-ubuntu-x64.tar.gz"
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${LLAMA_ARCHIVE}"
LLAMA_SHA256=6bf3d20de562e4df230f1a7c54fb7a06a80c7ff40f5311c953e8255744be4eb2
MODEL_NAME=qwen2.5-0.5b-instruct-q4_k_m.gguf
MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/${MODEL_NAME}"
MODEL_SHA256=74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db
MIN_AVAILABLE_KB=1100000
MIN_DISK_KB=1600000

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
[ "$(uname -m)" = "x86_64" ] || { printf 'unsupported architecture: %s\n' "$(uname -m)" >&2; exit 2; }
for command in curl sha256sum tar install systemctl awk; do
    command -v "$command" >/dev/null || { printf 'missing prerequisite: %s\n' "$command" >&2; exit 3; }
done
getent passwd finance-radar >/dev/null || { printf 'finance-radar user is missing\n' >&2; exit 3; }

available_kb=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
disk_kb=$(df -Pk /opt | awk 'NR==2 {print $4}')
printf 'resource_gate available_kb=%s required_kb=%s disk_kb=%s required_disk_kb=%s\n' \
    "$available_kb" "$MIN_AVAILABLE_KB" "$disk_kb" "$MIN_DISK_KB"
[ "$available_kb" -ge "$MIN_AVAILABLE_KB" ] || { printf 'resource gate failed: memory\n' >&2; exit 4; }
[ "$disk_kb" -ge "$MIN_DISK_KB" ] || { printf 'resource gate failed: disk\n' >&2; exit 4; }

stage=$(mktemp -d /tmp/finance-radar-evidence-llm.XXXXXX)
trap 'rm -rf "$stage"' EXIT
install -d -o finance-radar -g finance-radar \
    "$BASE/releases" "$BASE/models"

release="$BASE/releases/$TAG"
if [ ! -x "$release/llama-server" ]; then
    curl --fail --location --retry 3 --output "$stage/$LLAMA_ARCHIVE" "$LLAMA_URL"
    printf '%s  %s\n' "$LLAMA_SHA256" "$stage/$LLAMA_ARCHIVE" | sha256sum --check --status
    tar -xzf "$stage/$LLAMA_ARCHIVE" -C "$stage"
    [ -x "$stage/llama-$TAG/llama-server" ] || { printf 'llama-server missing from archive\n' >&2; exit 5; }
    mv "$stage/llama-$TAG" "$release"
fi
printf '%s  %s\n' "$LLAMA_SHA256" "$stage/$LLAMA_ARCHIVE" >/dev/null
ln -sfn "$release" "$BASE/current"

model="$BASE/models/$MODEL_NAME"
if ! printf '%s  %s\n' "$MODEL_SHA256" "$model" | sha256sum --check --status 2>/dev/null; then
    curl --fail --location --retry 3 --output "$stage/$MODEL_NAME" "$MODEL_URL"
    printf '%s  %s\n' "$MODEL_SHA256" "$stage/$MODEL_NAME" | sha256sum --check --status
    install -m 0644 -o finance-radar -g finance-radar "$stage/$MODEL_NAME" "$model"
fi
chown -R finance-radar:finance-radar "$BASE/releases" "$BASE/models"
install -m 0644 \
    /opt/finance-radar/current/deployment/systemd/finance-radar-evidence-llm.service \
    /etc/systemd/system/finance-radar-evidence-llm.service
systemctl daemon-reload

printf 'llama_release=%s\nmodel=%s\nmodel_sha256=%s\n' "$TAG" "$model" "$MODEL_SHA256"
if [ "$ACTIVATE" != "--activate" ]; then
    # Reinstalling model artifacts must not preserve an old boot-enabled model
    # service by accident.  The local model is optional and advisory only.
    systemctl disable --now finance-radar-evidence-llm.service >/dev/null 2>&1 || true
    printf 'installed_not_activated=true; service_disabled=true; rerun with --activate after review\n'
    exit 0
fi

if systemctl is-active --quiet finance-radar-worker.service || \
   systemctl is-active --quiet finance-radar-backup.service; then
    printf 'refusing evidence LLM activation while worker or backup is active; stop the conflicting job first\n' >&2
    exit 5
fi
systemctl enable --now finance-radar-evidence-llm.service
ready=false
for _ in $(seq 1 90); do
    if curl -fsS http://127.0.0.1:18601/health >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    systemctl disable --now finance-radar-evidence-llm.service >/dev/null 2>&1 || true
    printf 'model failed health gate and was disabled\n' >&2
    systemctl status finance-radar-evidence-llm.service --no-pager >&2 || true
    exit 6
fi
curl -fsS http://127.0.0.1:18601/v1/models
printf '\nactivation=PASS loopback=127.0.0.1:18601 advisory_only=true no_trading=true\n'
