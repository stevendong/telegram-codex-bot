#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请用 sudo 运行：sudo ./scripts/install-systemd.sh" >&2
  exit 1
fi

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
service_src="${project_dir}/telegram-codex-bot.service"
env_src="${project_dir}/telegram-codex-bot.env.example"
service_dst=/etc/systemd/system/telegram-codex-bot.service
env_dst=/etc/telegram-codex-bot.env

install -m 0644 "${service_src}" "${service_dst}"
if [[ ! -e ${env_dst} ]]; then
  install -m 0600 "${env_src}" "${env_dst}"
  echo "已创建 ${env_dst}，请先填写 Telegram Token 和用户 ID。"
else
  chmod 0600 "${env_dst}"
  echo "保留现有 ${env_dst}。"
fi

install -d -m 0700 -o ubuntu -g ubuntu /var/lib/telegram-codex-bot
systemctl daemon-reload
echo "配置完成后启动：sudo systemctl enable --now telegram-codex-bot"
