"""
Incident #2: Node NotReady (kubelet down).

Stops the kubelet process inside one of the kind worker node containers via
`docker exec`, causing the node to stop reporting Ready. This has to happen
outside the Kubernetes API (a node can't make itself NotReady through the
API), which is why this script shells out to Docker directly instead of
using the kubernetes client, unlike the other injectors.

Usage:
  python injector/node_not_ready.py [--node capstart-worker]
  python injector/node_not_ready.py --revert [--node capstart-worker]
"""
from __future__ import annotations

import argparse
import subprocess

DEFAULT_NODE = "capstart-worker"


def _docker_exec(node: str, *cmd: str) -> None:
    full = ["docker", "exec", node, *cmd]
    print(f"[injector] running: {' '.join(full)}")
    subprocess.run(full, check=True)


def inject(node: str) -> None:
    _docker_exec(node, "systemctl", "stop", "kubelet")
    print(f"Fault injected: kubelet stopped on node container '{node}'.")
    print("Watch: kubectl get nodes -w   (should flip to NotReady within ~40s)")


def revert(node: str) -> None:
    _docker_exec(node, "systemctl", "start", "kubelet")
    print(f"Reverted: kubelet restarted manually on node container '{node}' (bypassing the agent).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=DEFAULT_NODE, help="kind node container name")
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    revert(args.node) if args.revert else inject(args.node)
