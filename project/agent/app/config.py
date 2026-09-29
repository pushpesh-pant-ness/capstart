"""Central configuration for the remediation agent, all overridable via env vars."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # In-cluster DNS names when the agent runs as a pod (see agent/k8s/deployment.yaml).
    # Point these at localhost:9090 / :3100 instead if you `kubectl port-forward`
    # and run the agent locally (see instructions.md).
    prometheus_url: str = "http://prometheus.monitoring.svc.cluster.local:9090"
    loki_url: str = "http://loki.monitoring.svc.cluster.local:3100"

    # AWS Bedrock - Amazon Nova model used ONLY to generate human-readable
    # diagnosis text. It never decides or generates the remediation action.
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "amazon.nova-lite-v1:0"
    bedrock_enabled: bool = True

    audit_db_path: str = "/data/audit.db"

    # Set to False to use the local kubeconfig instead of in-cluster service account
    # (useful when running the agent process on your laptop against the kind cluster).
    kube_in_cluster: bool = True

    demo_namespace: str = "demo"

    http_timeout_seconds: float = 5.0


settings = Settings()
