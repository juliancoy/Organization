#!/usr/bin/env bash
# Executed only inside the fresh Multipass guest.
set -euo pipefail
test -f /etc/organization-disposable-vm || { echo 'Run this script through run.py --vm.' >&2; exit 1; }
cd /home/ubuntu/Organization
mkdir -p .vm-results
exec > >(tee .vm-results/bootstrap.log) 2>&1
# Use the same supported Node major as the local development containers.
sudo docker run --rm -v /usr/local:/target node:24-bookworm-slim sh -ec '
  cp /usr/local/bin/node /target/bin/node
  cp -a /usr/local/lib/node_modules /target/lib/
  ln -sf ../lib/node_modules/npm/bin/npm-cli.js /target/bin/npm
  ln -sf ../lib/node_modules/npm/bin/npx-cli.js /target/bin/npx
'
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r PIdP/requirements.txt playwright aiosqlite
for repo in . OrgPortal PIdP; do
  git -C "$repo" init -b main
  git -C "$repo" config user.name 'Disposable VM Test'
  git -C "$repo" config user.email 'vm-test@example.invalid'
  git -C "$repo" add .
  git -C "$repo" commit -m 'Current source snapshot for disposable VM tests'
done
for package in OrgPortal/web OrgPortal/org-worker OrgPortal/chat-worker PIdP/serverless; do
  npm --prefix "$package" ci
done
# Both the existing Python and JavaScript checks expect this browser path.
.venv/bin/python -m playwright install --with-deps chromium
browser=$(.venv/bin/python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()')
sudo ln -s "$browser" /usr/local/bin/google-chrome
sudo ln -s "$browser" /usr/bin/google-chrome
