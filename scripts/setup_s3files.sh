#!/usr/bin/env bash
# setup_s3files.sh — Phase 2: create S3 Files file system and mount target
#
# Run AFTER the first `cdk deploy` (Phase 1) completes.
# This script uses the CDK stack outputs to create the S3 Files resources
# via AWS CLI, then prints the Phase 3 deploy command with the FS ID.
#
# Usage:
#   bash scripts/setup_s3files.sh [REGION]
set -euo pipefail

REGION="${1:-ap-northeast-1}"
STACK="S3FilesBenchmarkStack"

echo "=== S3 Files Setup (Phase 2) ==="
echo "Region : $REGION"
echo "Stack  : $STACK"
echo ""

# ── Fetch outputs from Phase 1 CDK deploy ────────────────────────────────────
get_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

echo ">>> Fetching stack outputs …"
BUCKET_NAME=$(get_output StandardBucketName)
SERVICE_ROLE_ARN=$(get_output S3FilesServiceRoleArn)
SUBNET_ID=$(get_output SubnetId)
SG_ID=$(get_output S3FilesSgId)

BUCKET_ARN="arn:aws:s3:::${BUCKET_NAME}"

echo "  Bucket ARN       : $BUCKET_ARN"
echo "  Service Role ARN : $SERVICE_ROLE_ARN"
echo "  Subnet ID        : $SUBNET_ID"
echo "  Security Group   : $SG_ID"
echo ""

# ── Step 1: Create S3 Files file system ──────────────────────────────────────
echo ">>> [1/4] Creating S3 Files file system …"
CREATE_OUT=$(aws s3files create-file-system \
  --bucket       "$BUCKET_ARN" \
  --role-arn     "$SERVICE_ROLE_ARN" \
  --region       "$REGION" \
  --output json)

FS_ID=$(echo "$CREATE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin)['fileSystemId'])")
echo "  File system ID : $FS_ID"

# ── Step 2+3: Create mount target (retry on ConflictException = FS not ready) ─
# describe-file-systems is not a valid s3files subcommand, so we detect FS
# readiness implicitly: ConflictException means "still initialising", anything
# else is a real error.
echo ">>> [2/3] Creating mount target (retrying until FS is ready) …"
MT_CREATED=0
for i in $(seq 1 60); do
  OUTPUT=$(aws s3files create-mount-target \
    --file-system-id "$FS_ID" \
    --subnet-id      "$SUBNET_ID" \
    --security-group "$SG_ID" \
    --region         "$REGION" \
    --output json 2>&1) && { MT_CREATED=1; break; }

  if echo "$OUTPUT" | grep -q "ConflictException"; then
    echo "  [$i/60] FS still initialising — waiting 10s …"
    sleep 10
  else
    echo "ERROR creating mount target:"
    echo "$OUTPUT"
    exit 1
  fi
done

if [ "$MT_CREATED" -eq 0 ]; then
  echo "ERROR: mount target creation timed out after 600s"
  exit 1
fi
echo "  Mount target created."

# ── Step 3 (cont.): Wait for mount target to become available ────────────────
echo ">>> [3/3] Waiting for mount target to become available …"
for i in $(seq 1 60); do
  RAW=$(aws s3files list-mount-targets \
    --file-system-id "$FS_ID" --region "$REGION" --output json 2>/dev/null || echo '{}')

  # Print raw JSON on the first iteration so key names are visible if parsing fails
  [ "$i" -eq 1 ] && echo "  raw: $RAW"

  MT_STATUS=$(echo "$RAW" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# s3files CLI may use camelCase or PascalCase — try both
for list_key in ('MountTargets', 'mountTargets', 'Results', 'results'):
    mts = d.get(list_key, [])
    if mts:
        mt = mts[0]
        for status_key in ('status', 'Status', 'LifeCycleState', 'lifecycle', 'LifecycleState'):
            if status_key in mt:
                print(mt[status_key])
                sys.exit(0)
print('none')
" 2>/dev/null || echo "parse_error")

  echo "  [$i/60] status: $MT_STATUS"
  [ "$MT_STATUS" = "available" ] && break
  sleep 5
done

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== S3 Files setup complete ==="
echo ""
echo "File system ID : $FS_ID"
echo ""
echo ">>> Phase 3 — add S3 Files volume to ECS task and redeploy:"
echo ""
echo "    cdk deploy --context s3files_fs_id=$FS_ID --region $REGION"
echo ""
echo ">>> To tear down later, delete the file system first:"
echo ""
echo "    bash scripts/teardown_s3files.sh $REGION $FS_ID"
