"""
Incident #3: Service/Endpoints unreachable.

Patches the demo-web Service's selector to a value that matches no pods, so
its Endpoints object drops to zero addresses. The original selector is
stashed in an annotation (capstart.dev/original-selector) so the agent's
executor can restore it deterministically.

Usage:
  python injector/service_unreachable.py
  python injector/service_unreachable.py --revert
"""
from __future__ import annotations

import argparse
import json

from common import announce, core_v1

NAMESPACE = "demo"
SERVICE = "demo-web"
ANNOTATION = "capstart.dev/original-selector"


def inject() -> None:
    core = core_v1()
    svc = core.read_namespaced_service(SERVICE, NAMESPACE)
    original_selector = svc.spec.selector
    announce("stash original selector", json.dumps(original_selector))

    patch = {
        "metadata": {"annotations": {ANNOTATION: json.dumps(original_selector)}},
        "spec": {"selector": {"app": "no-such-pods"}},
    }
    core.patch_namespaced_service(SERVICE, NAMESPACE, patch)
    print(f"Fault injected: {NAMESPACE}/{SERVICE} selector broken, Endpoints will empty out.")
    print(f"Watch: kubectl get endpoints -n {NAMESPACE} {SERVICE} -w")


def revert() -> None:
    core = core_v1()
    svc = core.read_namespaced_service(SERVICE, NAMESPACE)
    annotations = svc.metadata.annotations or {}
    original = annotations.get(ANNOTATION)
    if not original:
        print("No stashed selector found - nothing to revert.")
        return
    patch = {
        "metadata": {"annotations": {ANNOTATION: None}},
        "spec": {"selector": json.loads(original)},
    }
    core.patch_namespaced_service(SERVICE, NAMESPACE, patch)
    print(f"Reverted {NAMESPACE}/{SERVICE} selector manually (bypassing the agent).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    revert() if args.revert else inject()
