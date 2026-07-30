#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT_DIR}/.venv/bin/python"
ROLE="${1:-}"
RUN_DIR_ARG="${2:-}"

if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

cd "${ROOT_DIR}"

bool_enabled() {
  local value="${1:-1}"
  [[ "${value}" != "0" && "${value}" != "false" && "${value}" != "no" ]]
}

sanitize_ref() {
  "${PY}" - "$1" <<'PY'
import re
import sys

value = sys.argv[1].strip()
value = re.sub(r"[^A-Za-z0-9._/-]+", "-", value)
value = re.sub(r"/+", "/", value).strip("./-")
print(value or "flower-logs")
PY
}

default_branch() {
  local branch=""
  branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
  if [[ -n "${branch}" ]]; then
    echo "${branch}"
    return
  fi
  branch="$(git branch --show-current 2>/dev/null || true)"
  if [[ -n "${branch}" ]]; then
    echo "${branch}"
    return
  fi
  echo "main"
}

resolve_run_dir() {
  "${PY}" - "${ROLE}" "${RUN_DIR_ARG}" <<'PY'
import sys
from pathlib import Path

role = sys.argv[1].strip()
arg = sys.argv[2].strip()
if arg:
    path = Path(arg)
    if not path.exists() or not path.is_dir():
        raise SystemExit(f"log directory does not exist: {path}")
    print(path)
    raise SystemExit(0)

root = Path("logs")
roles = []
if role:
    role = {"master": "central", "slave": "client"}.get(role, role)
    roles = [role]
else:
    roles = ["central", "client"]

dirs = []
for item in roles:
    base = root / item
    if base.exists():
        dirs.extend(path for path in base.iterdir() if path.is_dir())

if not dirs:
    raise SystemExit(f"no log directories found for {','.join(roles)}")

latest = max(dirs, key=lambda path: path.stat().st_mtime)
print(latest)
PY
}

path_info() {
  "${PY}" - "${ROOT_DIR}" "$1" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2])
if not path.is_absolute():
    path = root / path
path = path.resolve()
try:
    rel = path.relative_to(root)
except ValueError as exc:
    raise SystemExit(f"log directory is outside repo: {path}") from exc
print(f"{path}\t{rel.as_posix()}")
PY
}

ensure_git_identity() {
  local repo_dir="$1"
  if ! git -C "${repo_dir}" config user.name >/dev/null; then
    git -C "${repo_dir}" config user.name "MeshNet Log Runner"
  fi
  if ! git -C "${repo_dir}" config user.email >/dev/null; then
    git -C "${repo_dir}" config user.email "meshnet-logs@local.invalid"
  fi
}

github_repo_from_origin() {
  local origin_url
  origin_url="$(git remote get-url origin 2>/dev/null || true)"
  "${PY}" - "${origin_url}" <<'PY'
import re
import sys

url = sys.argv[1].strip().removesuffix(".git")
for pattern in (r"github\.com[:/](?P<repo>[^/]+/[^/]+)$",):
    match = re.search(pattern, url)
    if match:
        print(match.group("repo"))
        break
else:
    print("")
PY
}

open_pr() {
  local repo="$1"
  local base_branch="$2"
  local head="$3"
  local title="$4"
  local body="$5"
  if ! bool_enabled "${AUTO_LOG_PR:-1}"; then
    return 1
  fi
  if ! command -v gh >/dev/null 2>&1; then
    echo "[logs] gh not installed; cannot auto-open PR"
    return 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "[logs] gh is not authenticated; cannot auto-open PR"
    return 1
  fi
  gh pr create \
    --repo "${repo}" \
    --base "${base_branch}" \
    --head "${head}" \
    --title "${title}" \
    --body "${body}" \
    --draft
}

push_fork_and_pr() {
  local worktree="$1"
  local repo="$2"
  local base_branch="$3"
  local log_branch="$4"
  local title="$5"
  local body="$6"
  if ! bool_enabled "${AUTO_LOG_PR:-1}"; then
    return 1
  fi
  if ! command -v gh >/dev/null 2>&1; then
    echo "[logs] gh not installed; cannot fork/open PR"
    return 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "[logs] gh is not authenticated; cannot fork/open PR"
    return 1
  fi

  local login repo_name fork_remote fork_url
  login="$(gh api user -q .login)"
  repo_name="${repo##*/}"
  fork_remote="log-fork-${login}"
  fork_url="https://github.com/${login}/${repo_name}.git"

  if ! git -C "${worktree}" remote get-url "${fork_remote}" >/dev/null 2>&1; then
    gh repo fork "${repo}" --remote --remote-name "${fork_remote}" || true
    if ! git -C "${worktree}" remote get-url "${fork_remote}" >/dev/null 2>&1; then
      git -C "${worktree}" remote add "${fork_remote}" "${fork_url}"
    fi
  fi

  git -C "${worktree}" push -u "${fork_remote}" "${log_branch}"
  open_pr "${repo}" "${base_branch}" "${login}:${log_branch}" "${title}" "${body}"
}

RUN_DIR="$(resolve_run_dir)"
IFS=$'\t' read -r RUN_DIR_ABS RUN_DIR_REL < <(path_info "${RUN_DIR}")
LOG_ROLE="$(basename "$(dirname "${RUN_DIR_REL}")")"
RUN_NAME="$(basename "${RUN_DIR_REL}")"
BASE_BRANCH="${AUTO_LOG_BASE_BRANCH:-$(default_branch)}"
BASE_BRANCH="$(sanitize_ref "${BASE_BRANCH}")"
LOG_BRANCH="$(sanitize_ref "logs/${LOG_ROLE}/${RUN_NAME}")"
TITLE="Add Flower run logs ${RUN_NAME}"
BODY="Automated Flower round test log bundle from ${LOG_ROLE}.

Run directory:
\`${RUN_DIR_REL}\`

This PR contains diagnostic logs only."

echo "[logs] run_dir=${RUN_DIR_REL}"
echo "[logs] base_branch=${BASE_BRANCH}"
echo "[logs] log_branch=${LOG_BRANCH}"

if [[ ! -d .git ]]; then
  echo "[logs] not a git checkout; cannot publish logs"
  exit 0
fi

ORIGINAL_BRANCH="$(git branch --show-current 2>/dev/null || true)"
WORKTREE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/meshnet-log-worktree.XXXXXX")"
DIRECT_PUSHED=0
PUBLISH_DONE=0

cleanup() {
  git worktree remove --force "${WORKTREE_ROOT}" >/dev/null 2>&1 || rm -rf "${WORKTREE_ROOT}"
  local push_enabled=0
  if bool_enabled "${AUTO_LOG_PUSH:-1}"; then
    push_enabled=1
  fi
  if [[ "${PUBLISH_DONE}" == "1" || "${push_enabled}" == "0" ]]; then
    git branch -D "${LOG_BRANCH}" >/dev/null 2>&1 || true
  fi
  if [[ "${DIRECT_PUSHED}" == "1" && "${ORIGINAL_BRANCH}" == "${BASE_BRANCH}" ]]; then
    rm -rf "${RUN_DIR_ABS}" 2>/dev/null || true
    git pull --ff-only origin "${BASE_BRANCH}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

BASE_REF="HEAD"
if git fetch origin "${BASE_BRANCH}" >/dev/null 2>&1; then
  BASE_REF="origin/${BASE_BRANCH}"
else
  echo "[logs] could not fetch origin/${BASE_BRANCH}; using current HEAD as log base"
fi

if git show-ref --verify --quiet "refs/heads/${LOG_BRANCH}"; then
  git branch -D "${LOG_BRANCH}" >/dev/null 2>&1 || true
fi

rm -rf "${WORKTREE_ROOT}"
git worktree add -b "${LOG_BRANCH}" "${WORKTREE_ROOT}" "${BASE_REF}" >/dev/null
ensure_git_identity "${WORKTREE_ROOT}"

mkdir -p "${WORKTREE_ROOT}/$(dirname "${RUN_DIR_REL}")"
rm -rf "${WORKTREE_ROOT:?}/${RUN_DIR_REL}"
cp -a "${RUN_DIR_ABS}" "${WORKTREE_ROOT}/${RUN_DIR_REL}"

git -C "${WORKTREE_ROOT}" add -f "${RUN_DIR_REL}"
if git -C "${WORKTREE_ROOT}" diff --cached --quiet; then
  echo "[logs] nothing to commit"
  exit 0
fi

git -C "${WORKTREE_ROOT}" commit -m "${TITLE}"

if ! bool_enabled "${AUTO_LOG_PUSH:-1}"; then
  echo "[logs] AUTO_LOG_PUSH disabled; committed in temporary worktree only"
  exit 0
fi

if bool_enabled "${AUTO_LOG_DIRECT_PUSH:-1}"; then
  if git -C "${WORKTREE_ROOT}" push origin "HEAD:${BASE_BRANCH}"; then
    DIRECT_PUSHED=1
    PUBLISH_DONE=1
    echo "[logs] pushed logs directly to origin/${BASE_BRANCH}"
    exit 0
  fi

  echo "[logs] direct push to origin/${BASE_BRANCH} failed; retrying after fetch/rebase"
  if git -C "${WORKTREE_ROOT}" fetch origin "${BASE_BRANCH}" \
    && git -C "${WORKTREE_ROOT}" rebase "origin/${BASE_BRANCH}" \
    && git -C "${WORKTREE_ROOT}" push origin "HEAD:${BASE_BRANCH}"; then
    DIRECT_PUSHED=1
    PUBLISH_DONE=1
    echo "[logs] pushed logs directly to origin/${BASE_BRANCH} after rebase"
    exit 0
  fi
fi

REPO="$(github_repo_from_origin)"
if git -C "${WORKTREE_ROOT}" push -u origin "${LOG_BRANCH}"; then
  PUBLISH_DONE=1
  echo "[logs] pushed log branch to origin/${LOG_BRANCH}"
  if [[ -n "${REPO}" ]]; then
    if open_pr "${REPO}" "${BASE_BRANCH}" "${LOG_BRANCH}" "${TITLE}" "${BODY}"; then
      echo "[logs] opened PR from origin branch"
      exit 0
    fi
    echo "[logs] branch pushed; open PR manually for ${LOG_BRANCH} -> ${BASE_BRANCH}"
  fi
  exit 0
fi

echo "[logs] branch push to origin failed"
if [[ -n "${REPO}" ]] && push_fork_and_pr "${WORKTREE_ROOT}" "${REPO}" "${BASE_BRANCH}" "${LOG_BRANCH}" "${TITLE}" "${BODY}"; then
  PUBLISH_DONE=1
  echo "[logs] pushed fork branch and opened PR"
  exit 0
fi

echo "[logs] could not publish automatically"
echo "[logs] local log directory remains at ${RUN_DIR_REL}"
exit 0
