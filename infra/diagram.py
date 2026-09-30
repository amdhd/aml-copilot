"""Render infra/architecture.png from the two Terraform layers.

    uv run --no-project --with diagrams python infra/diagram.py

Needs graphviz (`brew install graphviz`). Not a project dependency: this runs
when the infra changes, not when the app does.

Solid clusters are infra/persistent, applied once. Dashed clusters are
infra/ephemeral, which exists only between `apply` and `destroy` around a demo.
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR, Fargate
from diagrams.aws.cost import Budgets
from diagrams.aws.database import RDSPostgresqlInstance
from diagrams.aws.management import Cloudwatch, SystemsManagerParameterStore
from diagrams.aws.network import ALB, InternetGateway, Route53
from diagrams.aws.security import CertificateManager, IAMRole
from diagrams.aws.storage import SimpleStorageServiceS3Bucket
from diagrams.onprem.client import Client, User
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.network import Internet

OUT = Path(__file__).with_name("architecture")

EPHEMERAL = {"style": "dashed", "bgcolor": "#FFF8EC", "pencolor": "#D98B00"}
PRIVATE = {"bgcolor": "#EEF5FF", "pencolor": "#3B7DD8"}
PUBLIC = {"bgcolor": "#F1F8EC", "pencolor": "#5A9E2F"}

# splines=spline, not ortho: graphviz cannot place edge labels on orthogonal
# edges, and draws them wherever there is room instead.
graph_attr = {"fontsize": "22", "labelloc": "t", "pad": "0.6",
              "nodesep": "0.7", "ranksep": "1.4", "splines": "spline"}

with Diagram("AML Investigation Copilot -- AWS (ECS Fargate)\n"
             "ap-southeast-1  |  solid = persistent layer (~$0.20/mo)  |  "
             "dashed = ephemeral layer (apply before a demo, destroy after)",
             filename=str(OUT), show=False, direction="LR",
             graph_attr=graph_attr):

    analyst = User("Reviewer\n(browser, login;\ndemo-up's IP only)")
    laptop = Client("Laptop\ndocker push / terraform")
    deepseek = Internet("DeepSeek API\ndeepseek-flash")

    with Cluster("AWS  --  account 149751500899"):
        ecr = ECR("ECR\naml-copilot\nimmutable tags, keep 3,\nscan on push")
        s3 = SimpleStorageServiceS3Bucket("S3 artifacts\n1.84GB graph cache")
        ssm = SystemsManagerParameterStore("SSM SecureString\nLLM API key,\nreviewer hashes")
        acm = CertificateManager("ACM certificate\nDNS-validated")
        dns = Route53("Route 53 (shared zone)\nvalidation CNAME;\nalias while up")
        logs = Cloudwatch("CloudWatch Logs\n7-day retention")
        budget = Budgets("Budget $20/mo\nemail at 50% / 100%")
        roles = IAMRole("IAM roles\nexecution + task")

        with Cluster("VPC 10.40.0.0/16  --  2 AZs, no NAT Gateway"):
            igw = InternetGateway("Internet\nGateway")

            with Cluster("Public subnets 10.40.0.0/24, 10.40.1.0/24", graph_attr=PUBLIC):
                with Cluster("ephemeral", graph_attr=EPHEMERAL):
                    alb = ALB("ALB :443 (:80 redirects)\nSG rules: this demo's IPs")
                    with Cluster("ECS task: api (on-demand)", graph_attr=EPHEMERAL):
                        api = Fargate("api\nFastAPI + UI")
                        redis = Redis("redis\njob queue sidecar")
                    worker = Fargate("ECS task: worker\nFargate Spot\narq + LangGraph + GAT")

            with Cluster("Private subnets 10.40.10.0/24, 10.40.11.0/24\nno route out",
                         graph_attr=PRIVATE):
                with Cluster("ephemeral", graph_attr=EPHEMERAL):
                    rds = RDSPostgresqlInstance("RDS Postgres 16\n+ pgvector\nseed: 1,080 txns,\n51 alerts, 48 chunks")

    analyst >> Edge(label="HTTPS") >> igw >> alb >> Edge(label=":8000") >> api
    api >> Edge(label="enqueue") >> redis
    worker >> Edge(label="dequeue", style="dashed") >> redis
    api >> Edge(label=":5432") >> rds
    worker >> Edge(label="cases, checkpoints,\nguidance search") >> rds
    worker >> Edge(label="graph on start-up") >> s3
    worker >> Edge(label="HTTPS, direct\n(public IP)") >> deepseek
    laptop >> Edge(label="push image") >> ecr
    laptop >> Edge(label="upload graph", style="dashed") >> s3
    ecr >> Edge(label="image pull", style="dotted") >> [api, worker]
    ssm >> Edge(label="key into env", style="dotted") >> worker
    ssm >> Edge(label="reviewers into env", style="dotted") >> api
    acm >> Edge(label="TLS", style="dotted") >> alb
    dns >> Edge(label="hostname", style="dotted") >> alb
    roles >> Edge(label="assumed by", style="dotted") >> [api, worker]
    [api, worker] >> Edge(style="dotted") >> logs
