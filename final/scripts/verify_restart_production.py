"""Coordinated cold snapshots and restart drill for this Saber deployment only."""
from deploy_worker_datastores import CONTROL, WORKERS, remote

STOP = """
import subprocess
subprocess.run(["systemctl","stop","agi-office-backend"],check=True)
print("Saber stopped for coordinated snapshot.")
"""
BACKUP = r'''
import hashlib,json,os,subprocess,tarfile
from pathlib import Path
kind=KIND
name="saber-"+kind
root=Path("/var/backups/agi-office-datastores/20260924-e")
root.mkdir(parents=True,mode=0o700,exist_ok=True)
archive=root/(kind+".tar.gz")
assert not archive.exists()
obj=json.loads(subprocess.check_output(["docker","inspect",name]))[0]
destination="/data" if kind=="neo4j" else "/usr/share/elasticsearch/data"
data=Path(next(m["Source"] for m in obj["Mounts"] if m["Destination"]==destination)).resolve()
assert data.is_relative_to(Path("/var/lib/docker/volumes"))
subprocess.run(["docker","stop","--time","30",name],check=True,stdout=subprocess.DEVNULL)
try:
 with tarfile.open(archive,"w:gz") as bundle:
  bundle.add(data,arcname="data")
  bundle.add("/opt/agi-office-datastores/"+kind,arcname="deployment")
 archive.chmod(0o600)
 with tarfile.open(archive) as bundle:
  assert bundle.getmember("data").isdir()
 result={"service":kind,"archive":str(archive),"sha256":hashlib.file_digest(archive.open("rb"),"sha256").hexdigest(),"bytes":archive.stat().st_size}
 (root/(kind+".json")).write_text(json.dumps(result,indent=2))
 print(json.dumps(result))
finally:
 subprocess.run(["docker","start",name],check=True,stdout=subprocess.DEVNULL)
'''
CONTROL_BACKUP = r'''
import json,os,shutil,sys
from pathlib import Path
sys.path.insert(0,"/opt/agi-office/current")
from scripts.backup_server import backup,verify_restore
os.umask(0o077)
audit=Path("/var/backups/agi-office/native-20260924-e")
result=backup("/opt/agi-office/current",audit/"database-backups",application_db="/var/lib/agi-office/application.db",run_db="/var/lib/agi-office/agent_runs.sqlite3",require_runs=True)
restore=verify_restore(result["archive"],audit/"restore-drill")
# The service is stopped, so these vector/data files form a coordinated snapshot.
shutil.copytree("/var/lib/agi-office",audit/"restart-state",symlinks=True)
output={"backup":result,"restore":restore,"coordinated_state":str(audit/"restart-state")}
(audit/"backup-restore.json").write_text(json.dumps(output,indent=2))
print(json.dumps(output))
'''
WAIT_DATABASES = r'''
import json,time
from pathlib import Path
import requests
from neo4j import GraphDatabase
v=dict(line.strip().split("=",1) for line in Path("/etc/agi-office/datastores.env").read_text().splitlines() if "=" in line)
for attempt in range(60):
 try:
  r=requests.get(v["AGI_ES_ADDRESSES"],auth=(v["AGI_ES_USERNAME"],v["AGI_ES_PASSWORD"]),timeout=3)
  r.raise_for_status()
  with GraphDatabase.driver(v["AGI_NEO4J_URI"],auth=(v["AGI_NEO4J_USER"],v["AGI_NEO4J_PASSWORD"]),connection_timeout=3) as d:
   with d.session() as s: assert s.run("RETURN 1 AS ok").single()["ok"]==1
  print("Both datastores responsive after restart.")
  break
 except Exception:
  time.sleep(2)
else: raise RuntimeError("Datastore warmup deadline exceeded")
'''
START = r'''
import json,subprocess,time,urllib.request
from pathlib import Path
subprocess.run(["systemctl","start","agi-office-backend"],check=True)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(60):
 try:
  with opener.open("http://172.17.0.1:18090/readyz",timeout=5) as r:
   if r.status==200:
    result={"ready_after_restart":True,"main_pid":int(subprocess.check_output(["systemctl","show","agi-office-backend","-p","MainPID","--value"]))}
    Path("/var/backups/agi-office/native-20260924-e/restart.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
    break
 except Exception: pass
 time.sleep(2)
else: raise RuntimeError("Saber readiness deadline exceeded")
'''


if __name__ == "__main__":
    print(remote(CONTROL, STOP).strip(), flush=True)
    try:
        for kind, host in WORKERS.items():
            print(remote(host, BACKUP.replace("KIND", repr(kind), 1), root=True, timeout=240).strip(), flush=True)
        # remote() uses system python; use the known isolated app venv for drivers.
        import subprocess
        from deploy_worker_datastores import SSH
        for code in (CONTROL_BACKUP, WAIT_DATABASES):
            result = subprocess.run(SSH + [CONTROL, "/opt/agi-office/venvs/native-20260924/bin/python -"],
                                    input=code, text=True, encoding="utf-8", capture_output=True, timeout=240)
            if result.returncode:
                raise RuntimeError(result.stderr[-2000:])
            print(result.stdout.strip(), flush=True)
    finally:
        print(remote(CONTROL, START, timeout=180).strip(), flush=True)
