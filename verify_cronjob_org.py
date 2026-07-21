import json
import os
import urllib.request

API_KEY = os.environ["CRONJOB_ORG_API_KEY"]
req = urllib.request.Request(
    "https://api.cron-job.org/jobs",
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read().decode("utf-8"))

print(f"Total jobs: {len(data.get('jobs', []))}")
for j in data.get("jobs", []):
    sched = j["schedule"]
    print(f"  [{j['jobId']}] {j['title']:24s} enabled={j['enabled']!s:5s} "
          f"hours={sched['hours']} minutes={sched['minutes']} wdays={sched['wdays']}")
