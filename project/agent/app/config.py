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

    # Optional LangSmith tracing of the diagnosis pipeline (context gathering +
    # Bedrock calls) - off by default. Also requires LANGSMITH_API_KEY to be set
    # (langsmith reads that directly from the environment). See app/observability.py.
    langsmith_enabled: bool = False
    langsmith_project: str = "capstart-remediation-agent"

    # How many past resolved/executed incidents of the same type to retrieve
    # as grounding examples for the Bedrock diagnosis prompt (0 disables it).
    diagnosis_history_examples: int = 3

    # Default OFF: the remediation action always comes from the deterministic
    # allow-list (remediation/templates.py), regardless of what the LLM says -
    # only its title/steps narrative is used. Set true to instead trust the
    # LLM's own action choice (still rejected by the guardrail node if it
    # isn't the one allow-listed action for the incident_type - see
    # app/graph/guardrail.py). See app/graph/nodes/plan.py.
    llm_authors_action: bool = False

    # PostgreSQL audit store (see agent/k8s/postgres.yaml) - durable across
    # agent pod restarts/redeploys, unlike the old SQLite-on-emptyDir file.
    postgres_host: str = "postgres.agent.svc.cluster.local"
    postgres_port: int = 5432
    postgres_db: str = "agent"
    postgres_user: str = "agent"
    postgres_password: str = "capstart-agent-demo"

    @property
    def database_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Set to False to use the local kubeconfig instead of in-cluster service account
    # (useful when running the agent process on your laptop against the kind cluster).
    kube_in_cluster: bool = True

    demo_namespace: str = "demo"

    http_timeout_seconds: float = 5.0


settings = Settings()
