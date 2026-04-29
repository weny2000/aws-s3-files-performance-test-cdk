---
name: Bug report
about: Something is broken or produces incorrect results
title: "[Bug] "
labels: bug
assignees: ""
---

## Describe the bug

A clear and concise description of what the bug is.

## Steps to reproduce

1. Run `cdk deploy …`
2. Run `bash scripts/run_task.sh …`
3. Observe error

## Expected behaviour

What you expected to happen.

## Actual behaviour

What actually happened. Include log output from CloudWatch or the SSM result if relevant.

## Environment

| Item | Value |
|------|-------|
| AWS Region | e.g. `ap-northeast-1` |
| CDK version | `cdk --version` |
| Python version | `python --version` |
| Docker version | `docker --version` |

## Additional context

Any other context (screenshots, CloudFormation events, etc.).
