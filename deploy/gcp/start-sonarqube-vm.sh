#!/usr/bin/env bash
# Starts the SonarQube VM back up. Docker's restart policy on the compose
# services (`restart: unless-stopped`) means SonarQube and Postgres come back
# up on their own once the Docker daemon is running -- no manual
# `docker compose up` needed after a stop/start cycle, as long as the daemon
# itself is enabled to start on boot (see GCP_DEPLOYMENT.md 1.3).
#
# Usage: ./start-sonarqube-vm.sh [VM_NAME] [ZONE]
# VM_NAME defaults to "sonarqube-vm"; ZONE falls back to $SONARQUBE_VM_ZONE
# if not passed as an argument.
set -euo pipefail

VM_NAME="${1:-${SONARQUBE_VM_NAME:-sonarqube-vm}}"
ZONE="${2:-${SONARQUBE_VM_ZONE:-}}"

if [[ -z "$ZONE" ]]; then
  echo "Usage: $0 [VM_NAME] ZONE   (or export SONARQUBE_VM_ZONE)" >&2
  exit 1
fi

echo "Starting ${VM_NAME} in ${ZONE}..."
gcloud compute instances start "$VM_NAME" --zone="$ZONE"

IP="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"

echo "Started. External IP: ${IP}"
echo
echo "If this VM has an EPHEMERAL external IP (the default), that address"
echo "changes on every stop/start -- check it still matches SONAR_BASE_URL"
echo "in the agent's .env / Cloud Run job env vars, and update it if not."
echo "To stop this from happening on every cycle, reserve a static IP once:"
echo "  gcloud compute addresses create sonarqube-vm-ip --region=REGION"
echo "  gcloud compute instances add-access-config ${VM_NAME} --zone=${ZONE} \\"
echo "    --access-config-name=External NAT --address=RESERVED_IP"
echo
echo "Give SonarQube 30-60s after this to finish starting before hitting :9000."
