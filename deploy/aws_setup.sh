#!/usr/bin/env bash
# FL 모의투자 봇을 AWS EC2 (Ubuntu 22.04 / 24.04) 에 설치
#   - 평일 뉴욕 09:20 (한국 23:20, 서머타임 22:20) 에 자동 시작 -> 장 마감 10분 전 청산 후 스스로 종료
#   - 시작 전에 GitHub 최신 코드를 받음 (git pull), 비정상 종료 시 1분 뒤 다시 켬 (상태 파일로 이어서)
#   - 휴장일은 봇이 알아서 안내 후 종료
# 실행:  bash deploy/aws_setup.sh      (텔레그램 설정은 ~/scanner/알림설정.json 을 따로 복사)
set -euo pipefail
REPO=https://github.com/foodf1220-png/scanner.git
DIR="$HOME/scanner"

sudo apt-get update -y
sudo apt-get install -y python3-venv git

# 메모리 1GB 서버용 스왑 2GB (pandas 가 여러 종목을 한꺼번에 다룰 때 대비)
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO" "$DIR"; fi
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q yfinance pandas

sudo tee /etc/systemd/system/fl-paper.service > /dev/null <<EOF
[Unit]
Description=FL paper trading bot (fl.py --paper)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
ExecStartPre=-/usr/bin/git -C $DIR pull --ff-only
ExecStart=$DIR/.venv/bin/python fl.py --paper
Restart=on-failure
RestartSec=60
EOF

sudo tee /etc/systemd/system/fl-paper.timer > /dev/null <<EOF
[Unit]
Description=Start FL paper bot on US trading days

[Timer]
OnCalendar=Mon..Fri *-*-* 09:20:00 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now fl-paper.timer
echo "설치 완료. 다음 실행: $(systemctl list-timers fl-paper.timer --no-pager | sed -n 2p)"
echo "기록 보기: journalctl -u fl-paper -f   (또는 $DIR/logs/)"
