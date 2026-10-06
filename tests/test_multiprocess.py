"""Two separate OS processes (simulating two service instances) share one
file database and race for the same campaign quota."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from app.api import Services
from app.storage import Storage

# Worker program run in a separate interpreter: shares only the DB file.
_WORKER_CODE = (
    "import json, sys\n"
    "from app.api import Services\n"
    "from app.storage import Storage\n"
    "from app.errors import QuotaExhaustedError, PerUserLimitError\n"
    "db_path, worker_id = sys.argv[1], sys.argv[2]\n"
    "svc = Services(Storage(db_path))\n"
    "issued = rejected = 0\n"
    "for i in range(10):\n"
    "    try:\n"
    "        svc.vouchers.issue(1, f'w{worker_id}-u{i}', f'mp-req-{worker_id}-{i}')\n"
    "        issued += 1\n"
    "    except (QuotaExhaustedError, PerUserLimitError):\n"
    "        rejected += 1\n"
    "print(json.dumps({'issued': issued, 'rejected': rejected}))\n"
)

def test_two_processes_share_quota(file_db):
    # setup: active campaign, quota 15, per-user high
    svc = Services(Storage(file_db))
    svc.campaigns.create_campaign(
        "multi", total_quota=15, per_user_quota=15,
        start_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    svc.campaigns.activate_due()
    svc.shutdown()

    env = dict(os.environ, PYTHONPATH=os.getcwd())
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER_CODE, file_db, str(w)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            text=True)
        for w in range(2)
    ]
    totals = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        totals.append(json.loads(out.strip().splitlines()[-1]))

    issued_total = sum(t["issued"] for t in totals)
    rejected_total = sum(t["rejected"] for t in totals)
    assert issued_total == 15, totals
    assert rejected_total == 5, totals

    verify = Services(Storage(file_db))
    assert verify.campaigns.get(1).issued_count == 15
    # no duplicate voucher ids / request ids
    rows = verify.storage.query(
        "SELECT COUNT(*) n, COUNT(DISTINCT request_id) d FROM vouchers")
    assert rows[0]["n"] == rows[0]["d"] == 15
    verify.shutdown()
