#!/usr/bin/env bash
# Launch Google Chrome, installing it on FIRST USE (Chrome is kept out of the base image to stay
# lean -- esp. for the CPU/Colab path). Wired into the openbox "Google Chrome" menu item. 'user'
# has passwordless sudo, so the install runs without a prompt; an xterm shows progress.
set -u

if ! command -v google-chrome >/dev/null 2>&1; then
  # Managed policy: no sign-in prompts / promos (applies to every launch).
  sudo mkdir -p /etc/opt/chrome/policies/managed 2>/dev/null || true
  sudo tee /etc/opt/chrome/policies/managed/desktopia.json >/dev/null 2>&1 <<'JSON' || true
{
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PromotionalTabsEnabled": false,
  "BrowserAddPersonEnabled": false,
  "MetricsReportingEnabled": false,
  "DefaultBrowserSettingEnabled": false
}
JSON
  # Visible progress while the .deb downloads + installs (first launch only).
  xterm -title "Installing Google Chrome (first use)" -geometry 80x20 -e bash -c '
    set -e
    echo "Downloading Google Chrome (first use only)..."
    curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    echo "Installing..."
    sudo apt-get install -y /tmp/chrome.deb || sudo apt-get install -y --fix-broken
    rm -f /tmp/chrome.deb
    echo "Done." ' 2>/dev/null \
  || {                                  # no xterm? fall back to a silent install
       curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
         && sudo apt-get install -y /tmp/chrome.deb; rm -f /tmp/chrome.deb; }
fi

command -v google-chrome >/dev/null 2>&1 || { echo "Chrome install failed" >&2; exit 1; }
exec google-chrome --no-sandbox --no-first-run --no-default-browser-check "$@"
