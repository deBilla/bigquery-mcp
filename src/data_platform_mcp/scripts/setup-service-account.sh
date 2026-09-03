#!/usr/bin/env bash
# Create the read-only service account this server impersonates.
#
# Why bother, when the server only issues SELECT statements? Because that is a
# promise about code, and this makes it a fact about the identity: an account
# holding only jobUser and dataViewer cannot write, whatever the code does and
# whatever roles the person running it happens to have. With --datasets it also
# moves the dataset allowlist out of this process and into a grant Google
# enforces.
#
# Usage:
#   data-platform-mcp setup --project my-project
#   data-platform-mcp setup --project my-project --datasets sales,marketing
#   data-platform-mcp setup --project my-project --dry-run
#
# Safe to re-run: every step is skipped if it is already in place.
set -euo pipefail

PROJECT=""
DATASETS=""
SA_NAME="data-platform-mcp-ro"
MEMBER=""
DRY_RUN=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'

Options:
  --project PROJECT_ID   GCP project holding the datasets (required)
  --datasets a,b,c       Grant dataViewer on these datasets only, instead of
                         project-wide. Strongly preferred: it is the allowlist,
                         enforced by IAM rather than by this server.
  --name NAME            Service account id (default: data-platform-mcp-ro)
  --member MEMBER        Who may impersonate it (default: your gcloud account),
                         e.g. user:a@b.com or group:data-team@b.com
  --dry-run              Print the commands without running them
  -h, --help             Show this help
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project)  PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --datasets) DATASETS="${2:?--datasets needs a value}"; shift 2 ;;
    --name)     SA_NAME="${2:?--name needs a value}"; shift 2 ;;
    --member)   MEMBER="${2:?--member needs a value}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$PROJECT" ] || { echo "error: --project is required" >&2; usage >&2; exit 2; }
command -v gcloud >/dev/null || { echo "error: gcloud is not on PATH" >&2; exit 1; }
command -v bq >/dev/null || { echo "error: bq is not on PATH (part of the gcloud SDK)" >&2; exit 1; }

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

if [ -z "$MEMBER" ]; then
  account="$(gcloud config get-value account 2>/dev/null || true)"
  [ -n "$account" ] && [ "$account" != "(unset)" ] || {
    echo "error: no active gcloud account; pass --member user:you@example.com" >&2
    exit 1
  }
  MEMBER="user:${account}"
fi

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would run: %s\n' "$*"
  else
    "$@"
  fi
}

echo "project:         $PROJECT"
echo "service account: $SA_EMAIL"
echo "impersonated by: $MEMBER"
echo "data access:     ${DATASETS:-project-wide (all datasets)}"
echo

# 1. The account itself.
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  echo "[ ok ] service account already exists"
else
  echo "[ .. ] creating service account"
  run gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT" \
    --display-name "data-platform-mcp read-only" \
    --description "Read-only BigQuery access for the data-platform-mcp server"
fi

# 2. Running a query needs jobUser on the project. It confers no data access on
#    its own, so it is safe to grant project-wide even when reads are scoped.
echo "[ .. ] granting roles/bigquery.jobUser on $PROJECT"
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/bigquery.jobUser \
  --condition None \
  --quiet >/dev/null

# 3. Reading data. Per-dataset where asked, because a project-wide dataViewer
#    grant makes the server's allowlist advisory rather than enforced.
if [ -n "$DATASETS" ]; then
  IFS=',' read -r -a _datasets <<< "$DATASETS"
  for ds in "${_datasets[@]}"; do
    ds="$(echo "$ds" | tr -d '[:space:]')"
    [ -n "$ds" ] || continue
    echo "[ .. ] granting roles/bigquery.dataViewer on ${PROJECT}:${ds}"
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '  would add %s as READER on %s\n' "$SA_EMAIL" "$ds"
      continue
    fi
    # bq has no add-iam-policy-binding for datasets, so the ACL is read,
    # amended and written back. Adding an entry that is already present would
    # be rejected, hence the membership check.
    tmp="$(mktemp)"
    bq show --project_id="$PROJECT" --format=prettyjson "${PROJECT}:${ds}" > "$tmp"
    python3 - "$tmp" "$SA_EMAIL" <<'PY'
import json, sys
path, email = sys.argv[1], sys.argv[2]
with open(path) as fh:
    dataset = json.load(fh)
access = dataset.setdefault("access", [])
if any(e.get("userByEmail") == email and e.get("role") in ("READER", "roles/bigquery.dataViewer")
       for e in access):
    print("  already a READER; leaving the ACL unchanged")
else:
    access.append({"role": "READER", "userByEmail": email})
    with open(path, "w") as fh:
        json.dump(dataset, fh)
    print("  added as READER")
PY
    bq update --project_id="$PROJECT" --source "$tmp" "${PROJECT}:${ds}" >/dev/null
    rm -f "$tmp"
  done
else
  echo "[ .. ] granting roles/bigquery.dataViewer on $PROJECT (all datasets)"
  echo "       Consider --datasets to scope this; see --help."
  run gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role roles/bigquery.dataViewer \
    --condition None \
    --quiet >/dev/null
fi

# 3b. Scheduled queries are a different API with a different permission, so
#     the querying roles above do not cover them. Optional: without it the two
#     scheduled-query tools report a clear error and everything else works.
echo "[ .. ] granting roles/bigquerydatatransfer.viewer on $PROJECT (scheduled queries)"
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/bigquerydatatransfer.viewer \
  --condition None \
  --quiet >/dev/null

# 4. Let the human impersonate it. Without this the server cannot mint a token
#    and fails at the first query with a 403 naming neither account nor role.
echo "[ .. ] granting roles/iam.serviceAccountTokenCreator on the account to $MEMBER"
run gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project "$PROJECT" \
  --member "$MEMBER" \
  --role roles/iam.serviceAccountTokenCreator \
  --quiet >/dev/null

cat <<EOF

Done. Add this to ~/.config/data-platform-mcp/config.toml:

    [environments.default]
    project = "$PROJECT"
    impersonate = "$SA_EMAIL"$([ -n "$DATASETS" ] && printf '\n    dataset_allowlist = [%s]' "$(echo "$DATASETS" | sed 's/[^,]*/"&"/g')")

Then check it:

    data-platform-mcp doctor
EOF
