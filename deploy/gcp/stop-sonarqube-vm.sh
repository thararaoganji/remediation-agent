#!/usr/bin/env bash
# Stops the SonarQube VM so Compute Engine billing for it stops accruing
# between sessions -- the boot/data disks still bill at rest, but that's a
# small fraction of the VM's running cost.
#
# Usage: ./stop-sonarqube-vm.sh [VM_NAME] [ZONE]
# VM_NAME defaults to "sonarqube-vm"; ZONE falls back to $SONARQUBE_VM_ZONE
# if not passed as an argument.
set -euo pipefail

VM_NAME="${1:-${SONARQUBE_VM_NAME:-sonarqube-vm}}"
ZONE="${2:-${SONARQUBE_VM_ZONE:-}}"

if [[ -z "$ZONE" ]]; then
  echo "Usage: $0 [VM_NAME] ZONE   (or export SONARQUBE_VM_ZONE)" >&2
  exit 1
fi

echo "Stopping ${VM_NAME} in ${ZONE}..."
gcloud compute instances stop "$VM_NAME" --zone="$ZONE"
echo "Stopped."
