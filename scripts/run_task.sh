#!/usr/bin/env bash
# Run from the project root: bash scripts/run_task.sh [REGION]
set -euo pipefail

REGION="${1:-ap-northeast-1}"
STACK="S3FilesBenchmarkStack"

echo ">>> Fetching stack outputs …"

get_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

CLUSTER_ID=$(get_output ClusterId)
TASK_DEF=$(get_output TaskDefArn)
SUBNET_ID=$(get_output SubnetId)
SG_ID=$(get_output TaskSgId)
LOG_GROUP=$(get_output LogGroupName)

echo "  Cluster  : $CLUSTER_ID"
echo "  TaskDef  : $TASK_DEF"
echo "  Subnet   : $SUBNET_ID"
echo "  SG       : $SG_ID"
echo "  LogGroup : $LOG_GROUP"
echo ""
echo ">>> Starting ECS Fargate task …"

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER_ID" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --region "$REGION" \
  --network-configuration \
    "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SG_ID],assignPublicIp=DISABLED}" \
  --query "tasks[0].taskArn" \
  --output text)

echo "  Task ARN: $TASK_ARN"
TASK_ID="${TASK_ARN##*/}"
echo ""

# aws ecs wait tasks-stopped times out at 100 × 6 s = 10 min, which is too short
# for large file sizes (1 GB / 10 GB). Poll manually with a 3-hour ceiling.
MAX_WAIT_SEC=10800   # 3 hours
INTERVAL_SEC=15
elapsed=0
echo ">>> Waiting for task to complete (up to ${MAX_WAIT_SEC}s) …"

while true; do
  TASK_STATUS=$(aws ecs describe-tasks \
    --cluster "$CLUSTER_ID" --tasks "$TASK_ARN" --region "$REGION" \
    --query "tasks[0].lastStatus" --output text 2>/dev/null || echo "UNKNOWN")

  if [ "$TASK_STATUS" = "STOPPED" ]; then
    echo "  Task stopped after ${elapsed}s."
    break
  fi

  if [ "$elapsed" -ge "$MAX_WAIT_SEC" ]; then
    echo "ERROR: Task did not stop within ${MAX_WAIT_SEC}s."
    echo "  Current status : $TASK_STATUS"
    echo "  Stop manually  : aws ecs stop-task --cluster $CLUSTER_ID --task $TASK_ARN --region $REGION"
    exit 1
  fi

  echo "  [${elapsed}s] status: $TASK_STATUS …"
  sleep "$INTERVAL_SEC"
  elapsed=$((elapsed + INTERVAL_SEC))
done

# ── Detailed task diagnosis ───────────────────────────────────────────────────
TASK_JSON=$(aws ecs describe-tasks \
  --cluster "$CLUSTER_ID" --tasks "$TASK_ARN" --region "$REGION" \
  --output json)

STOP_REASON=$(echo "$TASK_JSON" | python3 -c "
import sys, json
t = json.load(sys.stdin)['tasks'][0]
print(t.get('stoppedReason', 'unknown'))
")

STOP_CODE=$(echo "$TASK_JSON" | python3 -c "
import sys, json
t = json.load(sys.stdin)['tasks'][0]
c = t.get('containers', [{}])[0]
code = c.get('exitCode')
print(code if code is not None else 'None')
")

CONTAINER_REASON=$(echo "$TASK_JSON" | python3 -c "
import sys, json
t = json.load(sys.stdin)['tasks'][0]
c = t.get('containers', [{}])[0]
print(c.get('reason', ''))
")

echo ""
echo "  Stop reason    : $STOP_REASON"
echo "  Container code : $STOP_CODE"
[ -n "$CONTAINER_REASON" ] && echo "  Container reason: $CONTAINER_REASON"

# exitCode=None  → task stopped before container started (mount/image failure)
# exitCode=0     → success
# exitCode!=0    → container ran but failed
if [ "$STOP_CODE" = "0" ]; then
  echo ""
  echo ">>> Results from SSM /benchmark/results:"
  aws ssm get-parameter \
    --name "/benchmark/results" \
    --region "$REGION" \
    --query "Parameter.Value" \
    --output text | python3 "$(dirname "$0")/print_results.py"
  echo ""
  echo ">>> Full logs:"
  echo "    aws logs tail $LOG_GROUP --follow --region $REGION"
else
  echo ""
  echo "=== TASK FAILED ==="
  echo ""
  if [ "$STOP_CODE" = "None" ]; then
    echo "Exit code is null — the container never started."
    echo "Likely causes:"
    echo "  1. Volume mount failed (EFS or S3 Files mount target unreachable)"
    echo "  2. Container image pull failed"
    echo "  3. Resource initialisation error"
    echo ""
    echo "Check for mount errors in the ECS task events:"
    echo "  aws ecs describe-tasks --cluster $CLUSTER_ID --tasks $TASK_ARN \\"
    echo "    --region $REGION --query 'tasks[0].{stopReason:stoppedReason,containers:containers[*].reason}'"
  fi
  echo ""
  echo "CloudWatch logs (may be empty if container never started):"
  echo "  aws logs tail $LOG_GROUP --region $REGION"
  echo ""
  echo "Recent log events:"
  aws logs get-log-events \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name "bench/$TASK_ID" \
    --region "$REGION" \
    --limit 50 \
    --query "events[].message" \
    --output text 2>/dev/null || \
    echo "  (no log stream found — container did not start)"
  exit 1
fi
