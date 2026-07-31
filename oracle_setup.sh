#!/bin/bash
# One-shot Oracle setup for polymarket copy monitor
set -e

echo "=== Moving files ==="
mkdir -p ~/polymarket
mv -f /tmp/copy_monitor.py /tmp/copy_state.json ~/polymarket/ 2>/dev/null || true

echo "=== Installing Python + requests ==="
sudo dnf install -y python3 python3-pip 2>&1 | tail -3
pip3 install --user requests 2>&1 | tail -3

echo "=== Writing systemd service ==="
sudo tee /etc/systemd/system/polymarket-monitor.service > /dev/null <<'EOF'
[Unit]
Description=Polymarket Copy Trade Monitor
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/polymarket
Environment="COPY_EMAIL_TO=chrisgarceau6@gmail.com"
Environment="COPY_EMAIL_FROM=chrisgarceau6@gmail.com"
Environment="COPY_EMAIL_PASSWORD=aribcdalmwztgkfb"
ExecStart=/usr/bin/python3 /home/opc/polymarket/copy_monitor.py --daemon
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

echo "=== Enabling + starting service ==="
sudo systemctl daemon-reload
sudo systemctl enable polymarket-monitor
sudo systemctl restart polymarket-monitor
sleep 3

echo "=== Status ==="
sudo systemctl status polymarket-monitor --no-pager
echo ""
echo "=== DONE — service should be Active: active (running) above ==="
