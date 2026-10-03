"""Central configuration for the remediation agent, all overridable via env vars."""
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical .env location: project/.env, resolved from this file so it works no
# matter what CWD the agent is launched from. Fall back to a .env next to the
# process CWD too. In-cluster this file usually doesn't exist (env comes from the
# Kubernetes secret), and a missing path is a harmless no-op.
_PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"

# Load .env into os.environ at import time so libraries that read os.environ
# directly (langsmith SDK, LangChain auto-tracing) see the same values pydantic
# does - pydantic-settings only loads .env into the Settings object, not os.environ.
load_dotenv(_PROJECT_ENV)
load_dotenv()  # also honor a .env in the current working dir (e.g. agent/.env)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(str(_PROJECT_ENV), ".env"), extra="ignore")

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
    # Bedrock calls). Requires langsmith_api_key. observability.py bridges these
    # into os.environ so the langsmith SDK / LangChain auto-tracing can read them
    # even when they only live in .env (pydantic loads .env into Settings, not
    # into os.environ). See app/observability.py.
    langsmith_enabled: bool = True
    langsmith_project: str = "capstart-remediation-agent"
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # How many past resolved/executed incidents of the same type to retrieve
    # as grounding examples for the Bedrock diagnosis prompt (0 disables it).
    diagnosis_history_examples: int = 3

    # Hybrid historical retrieval: when on, the historical node embeds the
    # current incident's symptoms (Bedrock Titan) and uses pgvector semantic
    # recall, then re-ranks with the existing resource/severity heuristic.
    # Off by default -> the deterministic keyword path is unchanged. Requires
    # the pgvector extension (see agent/k8s/postgres.yaml) and bedrock_enabled.
    hybrid_retrieval_enabled: bool = False
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dim: int = 1024
    vector_recall_limit: int = 10  # ANN candidate pool size before heuristic re-rank
    hybrid_vector_weight: float = 0.5  # alpha: final = alpha*vector + (1-alpha)*heuristic

    # Log-driven trigger: in addition to Prometheus/Alertmanager metric alerts,
    # watch Loki for error log lines and open incidents from them. Off by
    # default so metric-only deployments are unchanged. See app/log_watcher.py.
    log_trigger_enabled: bool = False
    log_watch_namespaces: str = "demo"  # comma-separated list of namespaces to watch
    log_error_pattern: str = r"(?i)panic|exception|traceback|fatal|oomkilled|crashloopbackoff|error"
    log_poll_interval_seconds: float = 30.0
    log_lookback: str = "2m"  # Loki `since` window scanned each poll

    # Agentic log classification: when the deterministic keyword pass can't
    # place an error log line, ask the LLM to classify it into a known
    # incident_type with a confidence. Below the threshold it stays 'unknown'
    # and the graph escalates rather than guessing an action. Off by default.
    agentic_log_classification: bool = False
    log_classification_min_confidence: float = 0.5

    # Agency flags for the diagnosis graph (see app/graph). All default OFF so
    # the deterministic pipeline is unchanged until explicitly enabled.
    #   agentic_investigation - investigate node becomes a tool-calling ReAct
    #     loop that decides which read-only diagnostics to run (app/graph/tools.py)
    #   agentic_supervisor    - supervisor routes via an LLM decision instead of
    #     the P4+similarity rule (deterministic guards still backstop it)
    #   agent_max_steps       - hard cap on the agent's tool calls / loop hops
    agentic_investigation: bool = True
    agentic_supervisor: bool = True
    agent_max_steps: int = 6

    # agentic_reflection - a self-critique node re-checks the drafted plan's
    # coherence/evidence before the guardrail, and can loop back to investigate
    # (bounded by agent_max_steps). Default OFF.
    agentic_reflection: bool = True

    # Default OFF: the remediation action always comes from the deterministic
    # allow-list (remediation/templates.py), regardless of what the LLM says -
    # only its title/steps narrative is used. Set true to instead trust the
    # LLM's own action choice (still rejected by the guardrail node if it
    # isn't the one allow-listed action for the incident_type - see
    # app/graph/guardrail.py). See app/graph/nodes/plan.py.
    llm_authors_action: bool = True

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
