#!/usr/bin/env bash
# teardown_s3files.sh — delete S3 Files mount targets and file system
# Run BEFORE `cdk destroy` to prevent the "bucket has a file system attached" error.
#
# Usage:
#   bash scripts/teardown_s3files.sh [REGION] [FS_ID]
#   bash scripts/teardown_s3files.sh ap-northeast-1 fs-0123456789abcdef0
set -euo pipefail

REGION="${1:-ap-northeast-1}"
FS_ID="${2:-}"

if [ -z "$FS_ID" ]; then
  echo "Usage: bash scripts/teardown_s3files.sh REGION FS_ID"
  echo "  Find FS_ID: aws s3files list-file-systems --region REGION"
  exit 1
fi

echo "=== S3 Files Teardown ==="
echo "Region : $REGION"
echo "FS ID  : $FS_ID"
echo ""

echo ">>> [1/3] Deleting mount targets …"
MT_IDS=$(aws s3files list-mount-targets \
  --file-system-id "$FS_ID" --region "$REGION" \
  --query "MountTargets[].MountTargetId" --output text 2>/dev/null || echo "")

if [ -z "$MT_IDS" ] || [ "$MT_IDS" = "None" ]; then
  echo "  No mount targets found."
else
  for mt in $MT_IDS; do
    echo "  Deleting $mt …"
    aws s3files delete-mount-target \
      --mount-target-id "$mt" --region "$REGION" || true
  done

  echo ">>> [2/3] Waiting for mount targets to disappear …"
  for i in $(seq 1 60); do
    count=$(aws s3files list-mount-targets \
      --file-system-id "$FS_ID" --region "$REGION" \
      --query "length(MountTargets)" --output text 2>/dev/null || echo "0")
    [ "$count" = "0" ] && break
    echo "  Still $count MT(s) … ($i/60)"
    sleep 5
  done
fi

echo ">>> [3/3] Deleting file system $FS_ID …"
aws s3files delete-file-system \
  --file-system-id "$FS_ID" --region "$REGION" 2>/dev/null || \
  echo "  (already deleted or error — continuing)"

echo "  Waiting 20s for S3 to detach …"
sleep 20

echo ""
echo "=== Teardown complete ==="
echo "You can now run: cdk destroy --region $REGION"
