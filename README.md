# Autonomous Post-Deployment Incident & Remediation Agent
## 🚀 Overview

The Autonomous Post-Deployment Incident & Remediation Agent is an AI-powered AIOps platform designed to automatically detect, investigate, diagnose, and remediate post-deployment production incidents.

In a traditional environment, when something goes wrong after a deployment, engineers need to manually check alerts, logs, metrics, traces, deployment history, identify the root cause, decide what action to take, execute the fix, and verify that the system has recovered.

Our system automates this workflow using an AI Agent with human approval for production-impacting actions.

In simple terms:
```
Detect → Investigate → Diagnose → Plan → Approve → Remediate → Validate → Resolve
```

## 🏗️ What Are We Building?

The system monitors a running application using:
```
Logs → Grafana Loki
Metrics → Prometheus
Traces → OpenTelemetry
Alerts → Prometheus Alertmanager
```
When an incident is detected, the AI Incident Agent investigates the problem by collecting relevant operational data.

It then:
```
Analyzes the incident
Determines severity
Searches historical incidents
Performs Root Cause Analysis (RCA)
Creates a remediation plan
Requests human approval
Executes the approved action
Validates whether the issue is resolved
Records the complete incident and audit trail
```

## Architecture
```
Application
(FASTAPI app for detail log generation + Docker/Kubernetes)
        │
        ├──────── Logs ────────► Loki
        ├──────── Metrics ─────► Prometheus
                                  │
                                  ▼
                     Incident Detection
                     (Alertmanager)
                                  │
                                  ▼
                       AI Incident Agent
                    (LangGraph + LangChain)
                                  │
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
       Diagnosis             Severity             Historical
       (LLM + Tools)         (Rules + LLM)        (PostgreSQL
                                                  + pgvector)
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  ▼
                       Root Cause Analysis
                          (LLM + LangGraph)
                                  │
                                  ▼
                       Remediation Planning
                          (LLM + LangGraph)
                                  │
                                  ▼
                        Human Approval / HITL
                           (FastAPI + UI)
                                  │
                                  ▼
                       Automated Remediation
                    (Kubernetes / AWS / GitHub)
                                  │
                                  ▼
                    Post-Remediation Validation
                  (Prometheus + K8s Health Checks)
                                  │
                                  ▼
                       Incident Resolution
                           (FastAPI + DB)
                                  │
                                  ▼
                       Audit & Observability
                    (PostgreSQL + LangSmith/
                              LangFuse)
```

## 🔐 Human-in-the-Loop

The system is designed with controlled autonomy.

The AI does not receive unrestricted production access.

For potentially impactful actions:
```
AI Diagnosis
     ↓
Remediation Plan
     ↓
Human Approval
     ↓
Approved?
   ↙     ↘
 No       Yes
 ↓         ↓
Escalate  Execute
             ↓
         Validate
```
Only approved actions are executed through predefined and authorized tools.

## 🎯 Project Goal

The goal is to demonstrate an end-to-end Agentic AI + AIOps solution that can intelligently manage the post-deployment incident lifecycle.

Instead of:
```
Alert
  ↓
Engineer investigates
  ↓
Engineer finds root cause
  ↓
Engineer fixes issue
  ↓
Engineer verifies recovery

we aim for:

Alert
  ↓
AI investigates
  ↓
AI identifies probable cause
  ↓
AI proposes remediation
  ↓
Human approves
  ↓
AI executes
  ↓
AI validates
  ↓
Incident resolved
```
The project combines AI agents, observability, cloud infrastructure, Kubernetes, DevOps automation, human oversight, and production-style incident management into one system.
