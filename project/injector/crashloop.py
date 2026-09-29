"""
Incident #1 (Phase 1 - pipeline proof): Pod CrashLoopBackOff.

Injects a broken container command into the demo-web Deployment so every pod
it creates crashes immediately. The original command is stashed in a
Deployment annotation (capstart.dev/original-command) so the agent's
executor can restore it deterministically once a human approves the fix.

Usage:
  python injector/crashloop.py            # inject the fault
  python injector/crashloop.py --revert   # manually undo it (bypassing the agent)
"""
from __future__ import annotations

import argparse
import json

from common import announce, apps_v1

NAMESPACE = "demo"
DEPLOYMENT = "demo-web"
ANNOTATION = "capstart.dev/original-command"


def inject() -> None:
    apps = apps_v1()
    dep = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    container = dep.spec.template.spec.containers[0]

    original = {"name": container.name, "command": container.command, "args": container.args}
    announce("stash original command", json.dumps(original))

    patch = {
        "metadata": {"annotations": {ANNOTATION: json.dumps(original)}},
        "spec": {
            "template": {
                "spec": {"containers": [{"name": container.name, "command": ["sh", "-c", "echo boom; exit 1"]}]}
            }
        },
    }
    announce("patch deployment", f"{NAMESPACE}/{DEPLOYMENT} -> crash on start")
    apps.patch_namespaced_deployment(DEPLOYMENT, NAMESPACE, patch)
    print("Fault injected: demo-web pods will now CrashLoopBackOff.")
    print("Watch: kubectl get pods -n demo -l app=demo-web -w")


def revert() -> None:
    apps = apps_v1()
    dep = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    annotations = dep.metadata.annotations or {}
    original = annotations.get(ANNOTATION)
    if not original:
        print("No stashed original command found - nothing to revert (maybe the agent already fixed it).")
        return
    spec = json.loads(original)
    patch = {
        "metadata": {"annotations": {ANNOTATION: None}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": spec["name"], "command": spec.get("command"), "args": spec.get("args")}
                    ]
                }
            }
        },
    }
    apps.patch_namespaced_deployment(DEPLOYMENT, NAMESPACE, patch)
    print("Reverted demo-web to its original command manually (bypassing the agent).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    revert() if args.revert else inject()
