# Changelog

All notable changes to this project will be documented in this file.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).  
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-27

### Added
- Initial release
- ECS Fargate benchmark runner with four storage cases:
  - Case 1: S3 Standard → download to `/tmp` → local R/W
  - Case 2: S3 One Zone-IA → download to `/tmp` → local R/W
  - Case 3: EFS direct mount R/W
  - Case 4: S3 Express One Zone direct API R/W
- CDK stack (`infra/stack.py`) deploying VPC, EFS, S3 buckets, ECS cluster
- AZ ID custom resource for correct S3 Express directory bucket naming
- Per-iteration metrics: throughput (MB/s), P95/P99 latency, error rate
- Results persisted to SSM Parameter Store `/benchmark/results`
- `scripts/run_task.sh` helper to launch task and print summary table
- GitHub Actions lint workflow
- Issue templates for bug reports and feature requests
