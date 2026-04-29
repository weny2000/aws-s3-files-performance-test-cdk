#!/usr/bin/env bash
# Recover a stack stuck in ROLLBACK_FAILED or DELETE_FAILED because an
# orphaned S3 file system is attached to the S3 bucket.
#
# Usage:
#   bash scripts/recover_failed_stack.sh <REGION>
#
# The script finds the FS ID automatically from CloudFormation events or
# lets you pass it as a second argument:
#   bash scripts/recover_failed_stack.sh ap-northeast-1 fs-0278adf0311cb8fb5
set -euo pipefail

REGION="${1:-ap-northeast-1}"
FS_ID="${2:-}"
STACK="S3FilesBenchmarkStack"

echo "=== S3 Files Benchmark — Stack Recovery ==="
echo "Region : $REGION"
echo "Stack  : $STACK"
echo ""

# ── 0. Check stack status ─────────────────────────────────────────────────────
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "DOES_NOT_EXIST")

echo ">>> Stack status: $STACK_STATUS"

if [ "$STACK_STATUS" = "DOES_NOT_EXIST" ]; then
  echo "Stack does not exist. Nothing to recover."
  echo "Run: cdk deploy --region $REGION"
  exit 0
fi

# ── 1. Find the orphaned FS ID ────────────────────────────────────────────────
if [ -z "$FS_ID" ]; then
  echo ">>> [1/5] Searching for orphaned S3 file system …"

  # Try CloudFormation events first
  FS_ID=$(aws cloudformation describe-stack-events \
    --stack-name "$STACK" --region "$REGION" \
    --query "StackEvents[].ResourceStatusReason" --output text 2>/dev/null \
    | grep -oE 'fs-[0-9a-f]+' | head -1 || echo "")

  # Fallback: scan Lambda logs (most recent log stream)
  if [ -z "$FS_ID" ]; then
    LOG_GROUP=$(aws logs describe-log-groups --region "$REGION" \
      --log-group-name-prefix "/aws/lambda/${STACK}-S3FilesSetupFn" \
      --query "logGroups[0].logGroupName" --output text 2>/dev/null || echo "")

    if [ -n "$LOG_GROUP" ] && [ "$LOG_GROUP" != "None" ]; then
      FS_ID=$(aws logs filter-log-events \
        --log-group-name "$LOG_GROUP" --region "$REGION" \
        --filter-pattern '"fileSystemId"' \
        --query "events[].message" --output text 2>/dev/null \
        | grep -oE '"fileSystemId"\s*:\s*"(fs-[^"]+)"' \
        | grep -oE 'fs-[^"]+' | head -1 || echo "")
    fi
  fi

  # Fallback: list all S3 file systems
  if [ -z "$FS_ID" ]; then
    echo "  Could not auto-detect FS ID. Listing all S3 file systems:"
    aws s3files list-file-systems --region "$REGION" 2>/dev/null || true
    echo ""
    echo "  Run again with FS ID:"
    echo "    bash scripts/recover_failed_stack.sh $REGION fs-XXXXXXXXXX"
    exit 1
  fi
fi

echo "  FS ID: $FS_ID"
echo ""

# ── 2. Delete mount targets ───────────────────────────────────────────────────
echo ">>> [2/5] Deleting mount targets for $FS_ID …"
MT_IDS=$(aws s3files list-mount-targets \
  --file-system-id "$FS_ID" --region "$REGION" \
  --query "MountTargets[].MountTargetId" --output text 2>/dev/null || echo "")

if [ -z "$MT_IDS" ] || [ "$MT_IDS" = "None" ]; then
  echo "  No mount targets (already deleted)."
else
  for mt in $MT_IDS; do
    echo "  Deleting $mt …"
    aws s3files delete-mount-target \
      --mount-target-id "$mt" --region "$REGION" || true
  done

  echo ">>> [3/5] Waiting for mount targets to disappear …"
  for i in $(seq 1 60); do
    count=$(aws s3files list-mount-targets \
      --file-system-id "$FS_ID" --region "$REGION" \
      --query "length(MountTargets)" --output text 2>/dev/null || echo "0")
    [ "$count" = "0" ] && break
    echo "  Still $count MT(s) … ($i/60)"
    sleep 5
  done
fi

# ── 3. Delete file system ─────────────────────────────────────────────────────
echo ">>> [4/5] Deleting file system $FS_ID …"
aws s3files delete-file-system \
  --file-system-id "$FS_ID" --region "$REGION" 2>/dev/null || \
  echo "  (already deleted or error — continuing)"

echo "  Waiting 30s for S3 to detach …"
sleep 30

# ── 4. Delete (or continue-rollback on) the stack ────────────────────────────
echo ">>> [5/5] Cleaning up CloudFormation stack (status: $STACK_STATUS) …"

if [ "$STACK_STATUS" = "ROLLBACK_FAILED" ]; then
  # For ROLLBACK_FAILED, skip the bucket resource if it still can't be deleted,
  # then manually empty + delete the bucket afterward.
  BUCKET=$(aws cloudformation describe-stack-resources \
    --stack-name "$STACK" --region "$REGION" \
    --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
    --output text 2>/dev/null || echo "")

  echo "  Attempting delete-stack …"
  aws cloudformation delete-stack \
    --stack-name "$STACK" --region "$REGION"

  echo "  Waiting for stack deletion …"
  if ! aws cloudformation wait stack-delete-complete \
      --stack-name "$STACK" --region "$REGION" 2>/dev/null; then
    echo "  Stack deletion still failing. Retrying with --retain-resources …"
    aws cloudformation delete-stack \
      --stack-name "$STACK" --region "$REGION" \
      --retain-resources "${BUCKET:-StandardBucket7584F8B9}"
    aws cloudformation wait stack-delete-complete \
      --stack-name "$STACK" --region "$REGION"

    if [ -n "$BUCKET" ]; then
      echo ""
      echo "  Emptying and deleting retained bucket: $BUCKET"
      aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" 2>/dev/null || true
      aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null || \
        echo "  Bucket may already be empty/deleted."
    fi
  fi
else
  aws cloudformation delete-stack \
    --stack-name "$STACK" --region "$REGION"
  aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK" --region "$REGION"
fi

echo ""
echo "=== Recovery complete ==="
echo ""
echo "Redeploy with the fixed async Lambda code:"
echo "  cdk deploy --region $REGION"
