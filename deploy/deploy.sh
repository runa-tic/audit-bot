#!/usr/bin/env bash
# Install or update mm-audit on its own small VPS. Run from your own machine.
#
#   bash deploy/deploy.sh <ip> --pin                 # first contact: record the host key (read-only)
#   bash deploy/deploy.sh <ip> --check               # read-only: resources, unit state, health
#   bash deploy/deploy.sh <ip> --install <env-file>  # fresh host: harden, swap, user, venv, Chromium, env, unit, start
#   bash deploy/deploy.sh <ip> --update              # code only, then restart
#   bash deploy/deploy.sh <ip> --env <env-file>      # replace /etc/mm-audit/env, then restart
#   bash deploy/deploy.sh <ip> --brand <dir>         # install a brand (brand.json + logo) in /etc/mm-audit-brand
#   bash deploy/deploy.sh <ip> --stop                # systemctl disable --now (data kept)
#   bash deploy/deploy.sh <ip> --admin "<args>"      # python -m auditbot.admin <args>, as mmaudit with the env
#
# Key and pinned host key: MMA_KEY (default ~/.ssh/mmaudit_vps_ed25519) and MMA_KNOWN_HOSTS
# (default ~/.ssh/known_hosts_mmaudit, written by --pin). Everything after --pin is
# StrictHostKeyChecking=yes against that file.
# Touches only: sshd drop-in, ufw, /swapfile, /opt/mm-audit, /etc/mm-audit, /etc/mm-audit-brand, /var/lib/mm-audit, user mmaudit,
# unit mm-audit.
set -euo pipefail
cd "$(dirname "$0")/.."
IP="$1"; MODE="${2:---check}"; ENVF="${3:-}"
KEY="${MMA_KEY:-$HOME/.ssh/mmaudit_vps_ed25519}"
KH="${MMA_KNOWN_HOSTS:-$HOME/.ssh/known_hosts_mmaudit}"
T="root@$IP"
CP="/tmp/mma-%C"
# Connection sharing needs Unix sockets, which Git Bash's ssh on Windows lacks (2026-11-08, first box deploy)
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) MUX=() ;; *) MUX=(-o ControlMaster=auto -o ControlPath="$CP" -o ControlPersist=120) ;; esac
OPTS=(-i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=yes
      "${MUX[@]}" -o ConnectTimeout=15)
SSH=(ssh "${OPTS[@]}" "$T")
RSH="ssh ${OPTS[*]}"
REF=$(git rev-parse --short HEAD)

if [ "$MODE" = --pin ]; then
  [ -s "$KH" ] && grep -q "^$IP " "$KH" && { echo "already pinned in $KH:"; ssh-keygen -lf "$KH"; exit 0; }
  ssh-keyscan -t ed25519 -T 15 "$IP" 2>/dev/null > "$KH.new"
  [ -s "$KH.new" ] || { rm -f "$KH.new"; echo "no ed25519 host key from $IP"; exit 1; }
  cat "$KH.new" >> "$KH"; rm -f "$KH.new"; chmod 600 "$KH"
  echo "pinned:"; ssh-keygen -lf "$KH"
  ssh "${OPTS[@]}" "$T" "echo key login ok; hostname; . /etc/os-release; echo \$PRETTY_NAME"
  exit 0
fi

[ -z "$(git status --porcelain -- auditbot card deploy thresholds.json message_template.txt requirements.txt)" ] \
  || { echo "uncommitted changes: commit first"; exit 1; }

health() { "${SSH[@]}" "systemctl is-active mm-audit; journalctl -u mm-audit -n 15 --no-pager | sed -E 's/bot[0-9]+:[A-Za-z0-9_-]+/bot<redacted>/g'; \
  B=\$(grep -E '^AUDIT_API_BIND=' /etc/mm-audit/env | cut -d= -f2); curl -s -m 5 http://\${B:-127.0.0.1:8088}/v1/health; echo; \
  echo deployed \$(cat /opt/mm-audit/DEPLOYED_REF 2>/dev/null); free -m | sed -n 2p"; }

BRAND_DIR=/etc/mm-audit-brand  # outside /opt/mm-audit (rsync --delete) and /etc/mm-audit (700, root only)

push_env() {
  [ -f "$1" ] || { echo "missing env file $1"; exit 1; }
  rsync -e "$RSH" "$1" "$T:/etc/mm-audit/env.new"
  "${SSH[@]}" "chmod 600 /etc/mm-audit/env.new && chown root:root /etc/mm-audit/env.new && \
    { [ -f /etc/mm-audit/env ] && cp -p /etc/mm-audit/env /etc/mm-audit/env.bak-\$(date -u +%Y%m%d%H%M%S) || true; } && \
    mv /etc/mm-audit/env.new /etc/mm-audit/env"
}

case "$MODE" in
--check)
  "${SSH[@]}" "nproc; free -m; df -h / | tail -1; uptime; python3 --version; ufw status | head -3"
  health || true ;;
--install|--update)
  if [ "$MODE" = --install ]; then
    [ -f "$ENVF" ] || { echo "usage: deploy.sh <ip> --install <env-file>"; exit 1; }
    echo "== harden + base packages"
    "${SSH[@]}" "set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; \
      apt-get install -y -qq python3-venv rsync curl ufw unattended-upgrades >/dev/null; \
      printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/10-key-only.conf; \
      sshd -t; systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true; \
      sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '; \
      ufw allow OpenSSH >/dev/null; ufw --force enable >/dev/null; ufw status | head -4; \
      if ! swapon --show | grep -q .; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile; fi; \
      grep -q '^/swapfile' /etc/fstab || { [ -f /swapfile ] && echo '/swapfile none swap sw 0 0' >> /etc/fstab; } || true; swapon --show; \
      id mmaudit >/dev/null 2>&1 || useradd --system --home /var/lib/mm-audit --shell /usr/sbin/nologin mmaudit; \
      mkdir -p /opt/mm-audit /var/lib/mm-audit /etc/mm-audit && chmod 700 /etc/mm-audit && chown mmaudit:mmaudit /var/lib/mm-audit"
    echo "== key login still works after the sshd reload (fresh connection)"
    ssh "${OPTS[@]}" -o ControlPath=none "$T" true && echo ok
  fi
  if [ "$MODE" = --update ]; then
    # Never ship over a newer live tree: the ref running there must be in this checkout's history.
    LIVE=$("${SSH[@]}" "cat /opt/mm-audit/DEPLOYED_REF 2>/dev/null || true" | tr -d '[:space:]')
    if [ -z "$LIVE" ]; then
      echo "refused: the host has no /opt/mm-audit/DEPLOYED_REF, so what runs there is unknown."
      echo "A fresh host takes --install; on a known one, write the running ref to that file first."
      exit 1
    fi
    if ! git merge-base --is-ancestor "$LIVE" HEAD 2>/dev/null; then
      echo "refused: the live ref $LIVE is not in this checkout's history (HEAD $REF)."
      echo "Fetch and merge it first (git fetch origin; git merge-base --is-ancestor $LIVE HEAD must succeed)."
      exit 1
    fi
    # The cards' logo and colours come from a brand file (auditbot/brand.py). A host whose env names none would
    # switch every card to the default theme on this update, so that takes MMA_DEFAULT_BRAND=1.
    BRAND=$("${SSH[@]}" "sed -n 's/^AUDIT_BRAND=//p' /etc/mm-audit/env | tail -1" | tr -d '[:space:]')
    if [ -z "$BRAND" ] && [ "${MMA_DEFAULT_BRAND:-}" != 1 ]; then
      echo "refused: /etc/mm-audit/env sets no AUDIT_BRAND, so this update would put every card on the default theme."
      echo "Install a brand first (--brand <dir>, then AUDIT_BRAND=$BRAND_DIR/brand.json in the env via --env),"
      echo "or run with MMA_DEFAULT_BRAND=1 to mean it."
      exit 1
    fi
    if [ -n "$BRAND" ] && ! "${SSH[@]}" "test -r '$BRAND'"; then
      echo "refused: AUDIT_BRAND=$BRAND is not on the host; install it with --brand <dir> first."
      exit 1
    fi
  fi
  echo "== code $REF"
  CODE="auditbot card deploy thresholds.json message_template.txt requirements.txt"
  if command -v rsync >/dev/null; then
    rsync -a --delete --exclude .venv --exclude .browsers --exclude DEPLOYED_REF -e "$RSH" $CODE "$T:/opt/mm-audit/"
  else  # no local rsync (Git Bash on the box): ship HEAD's committed files; the VPS's rsync applies them with --delete
    git archive --format=tar HEAD -- $CODE | "${SSH[@]}" "set -e; S=\$(mktemp -d); tar -xf - -C \$S; cd \$S; \
      rsync -a --delete --exclude .venv --exclude .browsers --exclude DEPLOYED_REF $CODE /opt/mm-audit/; rm -rf \$S"
  fi
  if [ "$MODE" = --install ]; then
    echo "== venv + Chromium (a few minutes)"
    # Playwright's --with-deps knows LTS releases it was built for; on a newer Ubuntu it can refuse, so
    # fall back to Chromium's runtime libraries by name (each optional), then prove a real screenshot works.
    "${SSH[@]}" "set -e; export DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/opt/mm-audit/.browsers; cd /opt/mm-audit; \
      python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; \
      if ! .venv/bin/python -m playwright install --with-deps chromium >/tmp/pw-install.log 2>&1; then \
        echo 'playwright --with-deps refused (see /tmp/pw-install.log); installing Chromium libraries by name'; \
        for p in libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxkbcommon0 libxcomposite1 \
                 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 libatspi2.0-0t64 \
                 libxshmfence1 fonts-liberation; do apt-get install -y -qq \$p >/dev/null 2>&1 || echo \"  (no package \$p)\"; done; \
        .venv/bin/python -m playwright install chromium >/dev/null; fi; \
      apt-get install -y -qq fonts-inter >/dev/null 2>&1 || echo '  (no fonts-inter: cards use Google Fonts / system font)'; \
      .venv/bin/python -c \"from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage']); pg = b.new_page(); pg.set_content('<p>ok</p>')
    pg.screenshot(path='/tmp/pw-smoke.png'); b.close()
print('chromium screenshot ok')\""
    push_env "$ENVF"
    echo "== can this IP reach every venue? (some geo-block datacenter ranges)"
    "${SSH[@]}" "cd /opt/mm-audit && .venv/bin/python -c \"
from auditbot import venues
for v in ['binance','okx','bybit','gate','kucoin','kraken','mexc','bitget','htx','bingx']:
    try: r = venues.snapshot(v, 'ETH', 'USDT'); print(v, 'ok', round(r['spread_pct'], 4))
    except Exception as e: print(v, 'BLOCKED/ERR', type(e).__name__, str(e)[:80])
\""
  fi
  "${SSH[@]}" "set -e; cd /opt/mm-audit && .venv/bin/pip install -q -r requirements.txt; echo $REF > DEPLOYED_REF; \
    cp deploy/mm-audit.service /etc/systemd/system/mm-audit.service && systemctl daemon-reload && \
    systemctl enable -q mm-audit && systemctl restart mm-audit; sleep 8"
  health ;;
--env)
  push_env "$ENVF"
  "${SSH[@]}" "systemctl restart mm-audit; sleep 6"
  health ;;
--admin)  # bash deploy/deploy.sh <ip> --admin "<admin args>", e.g. "add-venue bitcoin kraken BTC USD"
  # systemd-run with the unit's user and env, so admin sees exactly what the service sees. Not sudo -E:
  # Ubuntu 26.04's sudo-rs ignores -E (2026-10-30), which silently dropped /etc/mm-audit/env.
  "${SSH[@]}" "systemd-run --quiet --wait --pipe --collect -p User=mmaudit -p Group=mmaudit \
    -p EnvironmentFile=/etc/mm-audit/env -p Environment=AUDIT_DATA=/var/lib/mm-audit -p Environment=HOME=/var/lib/mm-audit \
    -p Environment=PYTHONPATH=/opt/mm-audit -p WorkingDirectory=/opt/mm-audit \
    /opt/mm-audit/.venv/bin/python -m auditbot.admin $ENVF" ;;
--brand)  # bash deploy/deploy.sh <ip> --brand <dir>: brand.json + its logo, readable by the service; no restart
  [ -f "$ENVF/brand.json" ] || { echo "missing $ENVF/brand.json"; exit 1; }
  rsync -a --delete --chmod=D755,F644 -e "$RSH" "$ENVF/" "$T:$BRAND_DIR.new/"
  "${SSH[@]}" "set -e; chown -R root:root $BRAND_DIR.new; \
    { [ -d $BRAND_DIR ] && mv $BRAND_DIR $BRAND_DIR.bak-\$(date -u +%Y%m%d%H%M%S) || true; }; mv $BRAND_DIR.new $BRAND_DIR; ls -l $BRAND_DIR"
  echo "installed in $BRAND_DIR; it takes effect once the env has AUDIT_BRAND=$BRAND_DIR/brand.json and the service restarts" ;;
--stop)
  "${SSH[@]}" "systemctl disable --now mm-audit; systemctl is-active mm-audit || true" ;;
*) echo "unknown mode $MODE"; exit 1 ;;
esac
