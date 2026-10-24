#!/bin/bash
# Fix Windows CRLF line endings if present (file may be written on Windows)
if grep -qP '\r' "$0" 2>/dev/null; then
  sed -i 's/\r$//' "$0"
  exec "$0" "$@"
fi
# ─────────────────────────────────────────────────────────────────────────────
# CIS Pipeline Builder — Bootstrap Script
#
# This runs ONCE on first boot (via user-data/cloud-init).
# Installs: TAT Agent, Packer, Ansible, OpenSCAP, Goss, tccli
#
# NOTE: dnf update is NOT run here — do it manually after boot + reboot.
#       Reason: systemd/D-Bus scriptlet failures during cloud-init phase.
#       After login: dnf update -y && reboot
#
# PYTHON STRATEGY:
#   - python3 (3.9) = system Python — DO NOT REMOVE (dnf, cloud-init, firewalld depend on it)
#   - python3.12    = our tools (Ansible, tccli) — installed from AppStream
#   - Both coexist at /usr/bin/python3 and /usr/bin/python3.12
#
# Logs: /var/log/builder-bootstrap.log
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
exec >> /var/log/builder-bootstrap.log 2>&1

# Ensure HOME is set (cloud-init may not set it)
export HOME=/root

# Guard against re-execution (cloud-init may retry)
if [ -f /var/log/builder-bootstrap.complete ]; then
  echo "=== Bootstrap already completed, skipping — $(date) ==="
  exit 0
fi

# ── Fix broken cloud-init urllib3 dependency on RHEL 9 marketplace image ───────
pip3 install --force-reinstall urllib3 requests 2>/dev/null || true

echo "=== CIS Pipeline Builder Bootstrap — $(date) ==="

# ── [1/7] Wait for network ────────────────────────────────────────────────────
echo "[1/7] Waiting for internet connectivity..."
for i in $(seq 1 12); do
  if curl -fsS --max-time 5 https://mirrors.tencentyun.com > /dev/null 2>&1; then
    echo "  Internet reachable after $((i * 5)) seconds"
    break
  fi
  if [ "$i" -eq 12 ]; then
    echo "ERROR: No internet after 60s. Check NAT gateway and route table."
    ip route show
    cat /etc/resolv.conf
    exit 1
  fi
  echo "  Attempt $i/12 — waiting 5s..."
  sleep 5
done

dnf install -y wget

# ── [2/7] TAT Agent ───────────────────────────────────────────────────────────
echo "[2/7] Installing TAT Agent..."
if ! wget -qO - https://mirrors.tencentyun.com/install/tat_agent/tat_agent_installer.sh | sh; then
  echo "WARNING: TAT agent install failed — install manually later"
fi

# ── [3/7] Core packages (NO dnf update) ───────────────────────────────────────
echo "[3/7] Installing core packages..."
if ! dnf install -y \
  git \
  curl \
  wget \
  unzip \
  jq \
  vim \
  python3 \
  python3-pip \
  python3.12 \
  python3.12-pip \
  openscap-scanner \
  scap-security-guide; then
  echo "ERROR: Core package installation failed!"
  dnf repolist || true
  exit 1
fi

# ── [4/7] Python tools (Ansible + tccli) ──────────────────────────────────────
# ansible-core 2.16+ requires Python 3.10+. RHEL 9 default is 3.9.
# Using Python 3.12 from AppStream for Ansible.
echo "[4/7] Installing Python tools..."
python3.12 -m pip install --upgrade pip
if ! python3.12 -m pip install "ansible-core>=2.16,<2.17" tccli; then
  echo "ERROR: pip install failed!"
  exit 1
fi
ansible-galaxy collection install ansible.posix community.general

# ── [5/7] Packer ──────────────────────────────────────────────────────────────
echo "[5/7] Installing Packer..."
PACKER_VERSION="1.16.0"
if ! curl -fsSL "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_amd64.zip" \
  -o /tmp/packer.zip; then
  echo "ERROR: Failed to download Packer"
  exit 1
fi
unzip -o /tmp/packer.zip -d /usr/local/bin/
chmod +x /usr/local/bin/packer
rm -f /tmp/packer.zip

# HOME may not be set during cloud-init — Packer needs it for plugin directory
export HOME=/root
/usr/local/bin/packer plugins install github.com/hashicorp/tencentcloud
/usr/local/bin/packer plugins install github.com/hashicorp/ansible

# ── [6/7] Packer ──────────────────────────────────────────────────────────────
echo "[6/7] Installing Goss..."
GOSS_VERSION="v0.4.9"
if ! wget -O /usr/local/bin/goss \
  "https://github.com/goss-org/goss/releases/download/${GOSS_VERSION}/goss-linux-amd64"; then
  echo "WARNING: Goss download failed — install manually later"
else
  chmod +x /usr/local/bin/goss
fi

# ── [7/7] Configure workspace ─────────────────────────────────────────────────
echo "[7/7] Setting up workspace..."
export PATH="/usr/local/bin:$PATH"
# tccli configure set --region ap-singapore
mkdir -p /opt/cis-pipeline

# SSH hardening
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
systemctl restart sshd

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation Summary ==="
echo "Packer:      $(/usr/local/bin/packer version 2>/dev/null || echo 'NOT INSTALLED')"
echo "Ansible:     $(ansible --version 2>/dev/null | head -1 || echo 'NOT INSTALLED')"
echo "OpenSCAP:    $(oscap version 2>/dev/null | head -1 || echo 'NOT INSTALLED')"
echo "Goss:        $(/usr/local/bin/goss --version 2>/dev/null || echo 'NOT INSTALLED')"
echo "Python 3.9:  $(python3 --version 2>/dev/null || echo 'NOT INSTALLED') [system — do not remove]"
echo "Python 3.12: $(python3.12 --version 2>/dev/null || echo 'NOT INSTALLED') [ansible/tccli]"
echo "Git:         $(git --version 2>/dev/null || echo 'NOT INSTALLED')"
echo "tccli:       $(tccli --version 2>/dev/null || echo 'NOT INSTALLED')"
echo "TAT:         $(systemctl is-active tat_agent 2>/dev/null || echo 'NOT RUNNING')"
echo ""
echo "NEXT: Run 'dnf update -y && reboot' to apply security patches"
echo "=== Bootstrap Complete — $(date) ==="
touch /var/log/builder-bootstrap.complete
