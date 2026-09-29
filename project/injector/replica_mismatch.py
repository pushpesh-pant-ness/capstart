"""
Incident #5: Deployment replica mismatch.

Patches demo-api's container image to a tag that doesn't exist, so new pods
fail to pull the image (ImagePullBackOff) and status.availableReplicas falls
behind spec.replicas. The original image is stashed in an annotation
(capstart.dev/original-image) for deterministic rollback.

Usage:
  python injector/replica_mismatch.py
  python injector/replica_mismatch.py --revert
"""
from __future__ import annotations

import argparse

from common import announce, apps_v1

NAMESPACE = "demo"
DEPLOYMENT = "demo-api"
ANNOTATION = "capstart.dev/original-image"
BROKEN_IMAGE = "nginx:this-tag-does-not-exist"


def inject() -> None:
    apps = apps_v1()
    dep = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    container = dep.spec.template.spec.containers[0]
    original_image = container.image
    announce("stash original image", original_image)

    patch = {
        "metadata": {"annotations": {ANNOTATION: original_image}},
        "spec": {"template": {"spec": {"containers": [{"name": container.name, "image": BROKEN_IMAGE}]}}},
    }
    apps.patch_namespaced_deployment(DEPLOYMENT, NAMESPACE, patch)
    print(f"Fault injected: {NAMESPACE}/{DEPLOYMENT} image set to '{BROKEN_IMAGE}' (ImagePullBackOff).")
    print(f"Watch: kubectl get deployment -n {NAMESPACE} {DEPLOYMENT} -w")


def revert() -> None:
    apps = apps_v1()
    dep = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    annotations = dep.metadata.annotations or {}
    original_image = annotations.get(ANNOTATION)
    if not original_image:
        print("No stashed image found - nothing to revert.")
        return
    container_name = dep.spec.template.spec.containers[0].name
    patch = {
        "metadata": {"annotations": {ANNOTATION: None}},
        "spec": {"template": {"spec": {"containers": [{"name": container_name, "image": original_image}]}}},
    }
    apps.patch_namespaced_deployment(DEPLOYMENT, NAMESPACE, patch)
    print(f"Reverted {NAMESPACE}/{DEPLOYMENT} image manually (bypassing the agent).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    revert() if args.revert else inject()
