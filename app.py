#!/usr/bin/env python3
import aws_cdk as cdk
from infra.stack import S3FilesBenchmarkStack

app = cdk.App()
S3FilesBenchmarkStack(
    app, "S3FilesBenchmarkStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or "ap-northeast-1",
    ),
)
app.synth()
