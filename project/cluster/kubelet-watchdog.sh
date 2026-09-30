#!/bin/bash
# Runs inside each kind worker node container as a systemd service
# (kubelet-watchdog.service). Simulates real infrastructure self-healing
# (systemd Restart=, cloud provider node auto-repair): if kubelet has been
# stopped for longer than $THRESHOLD_SECONDS - no matter why (any injector
# script, or a human running `docker exec ... systemctl stop kubelet` by
# hand) - restart it. This guarantees node_not_ready always resolves within
# a bounded time on its own, so the agent's cordon/poll loop (and anyone
# testing the fault manually) never has to depend on someone remembering to
# run `--revert`.
THRESHOLD_SECONDS=${THRESHOLD_SECONDS:-90}
CHECK_INTERVAL_SECONDS=5

down_since=0
while true; do
    if systemctl is-active --quiet kubelet; then
        down_since=0
    else
        now=$(date +%s)
        if [ "$down_since" -eq 0 ]; then
            down_since=$now
        elif [ $((now - down_since)) -ge "$THRESHOLD_SECONDS" ]; then
            echo "[kubelet-watchdog] kubelet down for >= ${THRESHOLD_SECONDS}s, restarting it"
            systemctl start kubelet
            down_since=0
        fi
    fi
    sleep "$CHECK_INTERVAL_SECONDS"
done
