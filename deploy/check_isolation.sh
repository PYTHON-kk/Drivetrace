#!/bin/sh
# Negative isolation checks against the running compose deployment (Linux target).
# Each check must FAIL to do the forbidden thing; the script exits non-zero if any forbidden action succeeds.
#   sh deploy/check_isolation.sh
set -u
fail=0
run() { docker compose -f deploy/compose.yaml exec -T assistant python -c "$1" >/dev/null 2>&1; }

expect_blocked() {
  if run "$2"; then echo "FAIL  $1"; fail=1; else echo "ok    $1 (blocked)"; fi
}

expect_blocked "write to the data store" "open('/app/data/x', 'w')"
expect_blocked "write anywhere in the image" "open('/app/x', 'w')"
expect_blocked "open a CAN socket" "import socket; socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW).bind(('can0',))"
expect_blocked "outbound TCP (raw socket, bypassing the in-process guard)" "import _socket as s; c=s.socket(); c.settimeout(3); c.connect(('1.1.1.1', 443))"
expect_blocked "DNS lookup of an external name" "import socket; socket.getaddrinfo('example.com', 443)"
expect_blocked "gain privileges" "import os; os.setuid(0)"
expect_blocked "see vehicle devices" "import os, sys; sys.exit(0 if [d for d in os.listdir('/dev') if d.startswith(('can', 'ttyUSB', 'ttyACM'))] else 1)"

if [ "$fail" -eq 0 ]; then echo "all isolation checks passed"; fi
exit $fail
