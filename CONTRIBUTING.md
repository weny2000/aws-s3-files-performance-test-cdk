# Contributing

Thank you for considering a contribution! This document covers the process for reporting bugs, requesting features, and submitting pull requests.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Reporting Issues](#reporting-issues)
- [Development Setup](#development-setup)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Code Style](#code-style)

## Code of Conduct

Be respectful. Harassment of any kind will not be tolerated.

## Reporting Issues

Use the GitHub Issue templates:

- **Bug report** – something is broken or produces wrong results.
- **Feature request** – an idea for a new benchmark case or metric.

Before opening a new issue, please search existing ones to avoid duplicates.

## Development Setup

**Prerequisites:**

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| Node.js | 20+ (required by CDK CLI) |
| AWS CDK CLI | v2 (`npm install -g aws-cdk`) |
| Docker | 24+ |
| AWS CLI | v2 |

**Local install:**

```bash
git clone https://github.com/<your-org>/aws-s3-files-performance-test-cdk.git
cd aws-s3-files-performance-test-cdk

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Lint (runs in CI too):**

```bash
pip install ruff
ruff check .
```

**Synthesise without deploying** (validates CDK templates):

```bash
cdk synth
```

## Submitting a Pull Request

1. Fork the repository and create a branch from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make your changes. Keep commits focused; one logical change per commit.
3. Ensure `ruff check .` passes with zero errors.
4. Open a pull request against `main`. Fill in the PR template.
5. A maintainer will review and merge.

## Code Style

- **Python** – [`ruff`](https://docs.astral.sh/ruff/) with default settings. Max line length: **100**.
- **Shell scripts** – `bash`, `set -euo pipefail` at the top of every script.
- **CDK stacks** – one stack class per file under `infra/`.
- **No commented-out code** – delete it instead.
