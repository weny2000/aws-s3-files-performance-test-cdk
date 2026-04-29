"""
S3FilesBenchmarkStack
=====================
Benchmark cases:
  Case 1: Standard S3         → GetObject → /tmp → local R/W
  Case 2: S3 Express One Zone → GetObject → /tmp → local R/W
  Case 3: EFS                 → direct mount R/W
  Case 4: S3 Files            → direct mount R/W

Deployment is two-phase because CDK has no L2 support for S3 Files (2026-04):

  Phase 1 — cdk deploy
      Provisions VPC, S3, EFS, ECS.  Outputs the values needed for Phase 2.

  Phase 2 — bash scripts/setup_s3files.sh  (creates FS + mount target via CLI)
      Prints the FS ID, then runs:
      cdk deploy --context s3files_fs_id=fs-xxxx
      which adds the S3 Files volume to the ECS task definition.
"""
import os
from aws_cdk import (
    Stack, RemovalPolicy, CfnOutput, CustomResource, Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_s3 as s3,
    aws_s3express as s3express,
    aws_iam as iam,
    aws_logs as logs,
    aws_ssm as ssm,
    aws_lambda as lambda_,
    custom_resources as cr,
)
from constructs import Construct

# Inline Lambda: resolves AZ name (e.g. ap-northeast-1a) → AZ ID (e.g. apne1-az1)
_AZ_LOOKUP_CODE = """\
import boto3

def handler(event, context):
    if event['RequestType'] in ('Delete', 'Update'):
        return {
            'PhysicalResourceId': event.get('PhysicalResourceId', 'az-id-lookup'),
            'Data': {'AzId': ''},
        }
    az_name = event['ResourceProperties']['AzName']
    region  = event['ResourceProperties']['Region']
    resp = boto3.client('ec2', region_name=region).describe_availability_zones(
        Filters=[{'Name': 'zone-name', 'Values': [az_name]}]
    )
    az_id = resp['AvailabilityZones'][0]['ZoneId']
    return {
        'PhysicalResourceId': f'az-id-{az_name}',
        'Data': {'AzId': az_id},
    }
"""

# Path to the benchmark container source (relative to this file)
_BENCHMARK_DIR = os.path.join(os.path.dirname(__file__), "..", "benchmark")


class S3FilesBenchmarkStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 Files FS ID — supplied via CDK context after running setup_s3files.sh
        # e.g.: cdk deploy --context s3files_fs_id=fs-0123456789abcdef0
        s3files_fs_id: str = self.node.try_get_context("s3files_fs_id") or ""

        # ── VPC ──────────────────────────────────────────────────────────────
        vpc = ec2.Vpc(
            self, "BenchVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",  subnet_type=ec2.SubnetType.PUBLIC,              cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="Private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
            ],
        )

        # ── S3 Standard Bucket (Case 1 + S3 Files backing store) ─────────────
        # versioned=True is required by S3 Files
        standard_bucket = s3.Bucket(
            self, "StandardBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
        )

        # ── AZ ID lookup (required for S3 Express directory bucket name) ─────
        az_name = vpc.availability_zones[0]

        az_lookup_fn = lambda_.Function(
            self, "AzIdLookupFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(_AZ_LOOKUP_CODE),
        )
        az_lookup_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeAvailabilityZones"],
            resources=["*"],
        ))

        az_provider = cr.Provider(self, "AzProvider", on_event_handler=az_lookup_fn)
        az_cr = CustomResource(
            self, "AzIdCr",
            service_token=az_provider.service_token,
            properties={"AzName": az_name, "Region": self.region},
        )
        az_id = az_cr.get_att_string("AzId")   # e.g. apne1-az1

        # Case 2: S3 Express One Zone (Directory Bucket)
        directory_bucket_name = f"bench-s3express--{az_id}--x-s3"
        s3express.CfnDirectoryBucket(
            self, "ExpressDirectoryBucket",
            bucket_name=directory_bucket_name,
            data_redundancy="SingleAvailabilityZone",
            location_name=az_id,    # AZ ID required (e.g. apne1-az1)
        )

        # ── EFS (Case 3) ─────────────────────────────────────────────────────
        efs_sg = ec2.SecurityGroup(
            self, "EfsSg", vpc=vpc, description="EFS SG", allow_all_outbound=True)
        efs_fs = efs.FileSystem(
            self, "BenchEfs",
            vpc=vpc, security_group=efs_sg,
            removal_policy=RemovalPolicy.DESTROY,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.ELASTIC,
            encrypted=True,
        )
        efs_ap = efs_fs.add_access_point(
            "BenchAP", path="/benchmark",
            create_acl=efs.Acl(owner_gid="1000", owner_uid="1000", permissions="755"),
            posix_user=efs.PosixUser(gid="1000", uid="1000"),
        )

        # ── S3 Files (Case 4) — provisioned externally via setup_s3files.sh ──
        # IAM role that S3 Files assumes to read/write the bucket.
        # Its ARN is output below so the setup script can pass it to
        # `aws s3files create-file-system --role-arn`.
        s3_files_service_role = iam.Role(
            self, "S3FilesServiceRole",
            assumed_by=iam.ServicePrincipal("elasticfilesystem.amazonaws.com"),
        )
        standard_bucket.grant_read_write(s3_files_service_role)

        # Security group for the S3 Files mount target (NFS port 2049).
        s3files_sg = ec2.SecurityGroup(
            self, "S3FilesSg", vpc=vpc, description="S3 Files mount target SG",
            allow_all_outbound=True)

        # ── ECS Cluster ──────────────────────────────────────────────────────
        cluster = ecs.Cluster(self, "BenchCluster", vpc=vpc, container_insights=True)

        # ── IAM ──────────────────────────────────────────────────────────────
        task_role = iam.Role(
            self, "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMFullAccess")],
        )
        standard_bucket.grant_read_write(task_role)
        task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3express:CreateSession",
                "s3express:GetObject",
                "s3express:PutObject",
                "s3express:DeleteObject",
                "s3express:ListAllMyDirectoryBuckets",
            ],
            resources=[
                f"arn:aws:s3express:{self.region}:{self.account}:bucket/{directory_bucket_name}",
                f"arn:aws:s3express:{self.region}:{self.account}:bucket/{directory_bucket_name}/*",
            ],
        ))
        efs_fs.grant_root_access(task_role)

        # ── CloudWatch Logs ──────────────────────────────────────────────────
        log_group = logs.LogGroup(
            self, "BenchLogGroup",
            log_group_name="/ecs/s3files-benchmark",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Task Definition ──────────────────────────────────────────────────
        # ECS Fargate does not support S3 Files NFS volumes via EfsVolumeConfiguration.
        # Case 4 uses direct S3 GetObject/PutObject (no /tmp copy) instead.
        task_def = ecs.FargateTaskDefinition(
            self, "BenchTaskDef",
            cpu=2048,
            memory_limit_mib=4096,
            task_role=task_role,
            volumes=[
                ecs.Volume(
                    name="efs-bench",
                    efs_volume_configuration=ecs.EfsVolumeConfiguration(
                        file_system_id=efs_fs.file_system_id,
                        transit_encryption="ENABLED",
                        authorization_config=ecs.AuthorizationConfig(
                            access_point_id=efs_ap.access_point_id, iam="ENABLED"),
                    ),
                ),
            ],
        )

        container = task_def.add_container(
            "BenchContainer",
            image=ecs.ContainerImage.from_asset(_BENCHMARK_DIR),
            environment={
                "STANDARD_BUCKET": standard_bucket.bucket_name,
                "EXPRESS_BUCKET":  directory_bucket_name,
                "EFS_MOUNT":       "/mnt/efs",
                "RESULT_PARAM":    "/benchmark/results",
                "AWS_REGION":      self.region,
                "FILE_SIZES_MB":   "10240",
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="bench", log_group=log_group),
            essential=True,
        )

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/mnt/efs", source_volume="efs-bench", read_only=False)
        )

        # ── Security Groups ──────────────────────────────────────────────────
        task_sg = ec2.SecurityGroup(
            self, "TaskSg", vpc=vpc, description="ECS task SG", allow_all_outbound=True)
        efs_sg.add_ingress_rule(
            peer=task_sg, connection=ec2.Port.tcp(2049), description="ECS to EFS")
        s3files_sg.add_ingress_rule(
            peer=task_sg, connection=ec2.Port.tcp(2049), description="ECS to S3 Files")

        # ── SSM parameter (result store) ─────────────────────────────────────
        result_param = ssm.StringParameter(
            self, "ResultParam",
            parameter_name="/benchmark/results",
            string_value="pending",
        )
        result_param.grant_read(task_role)
        result_param.grant_write(task_role)

        # ── Outputs ──────────────────────────────────────────────────────────
        CfnOutput(self, "ClusterId",             value=cluster.cluster_name)
        CfnOutput(self, "TaskDefArn",            value=task_def.task_definition_arn)
        CfnOutput(self, "StandardBucketName",    value=standard_bucket.bucket_name)
        CfnOutput(self, "ExpressBucketName",     value=directory_bucket_name,
                  description="S3 Express One Zone Directory Bucket - Case 2")
        CfnOutput(self, "EfsId",                 value=efs_fs.file_system_id)
        # S3 Files setup outputs — used by scripts/setup_s3files.sh
        CfnOutput(self, "S3FilesServiceRoleArn", value=s3_files_service_role.role_arn,
                  description="Pass as --role-arn to: aws s3files create-file-system")
        CfnOutput(self, "S3FilesSgId",           value=s3files_sg.security_group_id,
                  description="Pass as --security-group to: aws s3files create-mount-target")
        CfnOutput(self, "TaskRoleArn",           value=task_role.role_arn)
        CfnOutput(self, "LogGroupName",          value=log_group.log_group_name)
        CfnOutput(self, "SubnetId",              value=vpc.private_subnets[0].subnet_id)
        CfnOutput(self, "TaskSgId",              value=task_sg.security_group_id)
        if s3files_fs_id:
            CfnOutput(self, "S3FilesFsId",       value=s3files_fs_id)
