"""Provision Saber-only loopback datastores and dedicated SSH tunnels.

Run on the Windows control workstation. Existing unrelated containers are untouched.
Secrets are generated on workers, transferred only through verified SSH pipes,
and written to root-only environment files; never printed or put in arguments.
"""
import json
import subprocess

CONTROL = "root@120.24.187.205"
WORKERS = {"elasticsearch": "ubuntu@106.52.176.128", "neo4j": "ubuntu@134.175.136.160"}
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"]


def remote(host, script, *, root=False, timeout=180):
    command = "sudo -n python3 -" if root else "python3 -"
    result = subprocess.run(SSH + [host, command], input=script, text=True, encoding="utf-8",
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Scripts never print credentials; avoid echoing their input in exceptions.
        raise RuntimeError(host + ": " + result.stderr[-2500:])
    return result.stdout


WORKER_SCRIPT = r'''
import json, os, secrets, subprocess
from pathlib import Path
os.umask(0o077)
kind = KIND
root = Path("/opt/agi-office-datastores") / kind
root.mkdir(parents=True, exist_ok=True)
secret = root / "credentials.json"
if not secret.exists():
    secret.write_text(json.dumps({"username": "elastic" if kind == "elasticsearch" else "neo4j",
                                 "password": secrets.token_urlsafe(36)}))
credentials = json.loads(secret.read_text())
if kind == "elasticsearch":
    image = "docker.elastic.co/elasticsearch/elasticsearch:8.19.22"
    content = """services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.22
    container_name: saber-elasticsearch
    restart: unless-stopped
    ports: ["127.0.0.1:19200:9200"]
    environment:
      discovery.type: single-node
      xpack.security.enabled: "true"
      xpack.security.http.ssl.enabled: "false"
      xpack.security.transport.ssl.enabled: "false"
      xpack.ml.enabled: "false"
      ingest.geoip.downloader.enabled: "false"
      ES_JAVA_OPTS: "-Xms384m -Xmx384m"
      ELASTIC_PASSWORD: ${DB_PASSWORD}
    volumes: ["data:/usr/share/elasticsearch/data"]
    mem_limit: 1g
    memswap_limit: 1g
    cpus: 0.75
    pids_limit: 256
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
volumes:
  data:
"""
else:
    image = "neo4j:5.26.29-community"
    content = """services:
  neo4j:
    image: neo4j:5.26.29-community
    container_name: saber-neo4j
    restart: unless-stopped
    ports: ["127.0.0.1:17687:7687"]
    environment:
      NEO4J_AUTH: neo4j/${DB_PASSWORD}
      NEO4J_server_memory_heap_initial__size: 256m
      NEO4J_server_memory_heap_max__size: 384m
      NEO4J_server_memory_pagecache_size: 128m
      NEO4J_server_http_enabled: "false"
      NEO4J_server_https_enabled: "false"
      NEO4J_server_bolt_advertised__address: "127.0.0.1:17687"
      NEO4J_dbms_usage__report_enabled: "false"
      NEO4J_dbms_security_procedures_allowlist: apoc.path.expandConfig
      NEO4J_dbms_security_procedures_unrestricted: apoc.path.expandConfig
    volumes: ["data:/data", "logs:/logs", "./plugins:/plugins:ro"]
    mem_limit: 768m
    memswap_limit: 768m
    cpus: 0.75
    pids_limit: 256
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
volumes:
  data:
  logs:
"""
# Pin verified pulled image digest; abort if pull hasn't completed.
info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
digest = info["RepoDigests"][0]
content = content.replace("image: " + image, "image: " + digest)
if kind == "neo4j":
    plugins = root / "plugins"
    plugins.mkdir(exist_ok=True)
    plugins.chmod(0o755)
    jar = plugins / "apoc-5.26.29-core.jar"
    if not jar.exists():
        jar.write_bytes(subprocess.check_output([
            "docker", "run", "--rm", "--memory", "64m", "--entrypoint", "cat", digest,
            "/var/lib/neo4j/labs/" + jar.name,
        ]))
    jar.chmod(0o644)
(root / ".env").write_text("DB_PASSWORD=" + credentials["password"] + "\n")
(root / "compose.yaml").write_text(content)
subprocess.run(["docker", "compose", "--project-name", "saber-" + kind,
                "-f", str(root / "compose.yaml"), "up", "-d", "--pull", "never"],
               check=True, stdout=subprocess.DEVNULL)
print(json.dumps({"service": kind, "image": digest}))
'''


def install_tunnels():
    public = remote(CONTROL, r'''
import subprocess
from pathlib import Path
root = Path("/etc/agi-office/tunnels")
root.mkdir(mode=0o700, exist_ok=True)
key = root / "id_ed25519"
if not key.exists():
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
print(key.with_suffix(".pub").read_text().strip())
''').strip()
    assert public.startswith("ssh-ed25519 ")
    known = []
    for kind, host in WORKERS.items():
        port = 19200 if kind == "elasticsearch" else 17687
        entry = 'restrict,port-forwarding,command="/bin/false",permitopen="127.0.0.1:%s" %s saber-%s-tunnel' % (port, " ".join(public.split()[:2]), kind)
        remote(host, r"""
from pathlib import Path
p = Path.home() / ".ssh" / "authorized_keys"
p.parent.mkdir(mode=0o700, exist_ok=True)
old = p.read_text() if p.exists() else ""
entry = %r
if entry not in old.splitlines():
    p.write_text(old.rstrip() + "\n" + entry + "\n")
p.chmod(0o600)
""" % entry)
        ip = host.split("@")[1]
        # Reuse previously verified host identities, not unauthenticated ssh-keyscan.
        lines = subprocess.check_output(["ssh-keygen", "-F", ip], text=True).splitlines()
        keys = [line for line in lines if line and not line.startswith("#")]
        assert keys, "No verified host identity"
        known.extend(keys)
    remote(CONTROL, """
from pathlib import Path
p = Path("/etc/agi-office/tunnels/known_hosts")
p.write_text(%r)
p.chmod(0o600)
""" % ("\n".join(known) + "\n"))
    for kind, host in WORKERS.items():
        port = 19200 if kind == "elasticsearch" else 17687
        unit = f"""[Unit]
Description=Saber {kind} private SSH tunnel
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
Type=simple
ExecStart=/usr/bin/ssh -NT -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/agi-office/tunnels/known_hosts -o IdentitiesOnly=yes -i /etc/agi-office/tunnels/id_ed25519 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:{port}:127.0.0.1:{port} {host}
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
[Install]
WantedBy=multi-user.target
"""
        remote(CONTROL, """
from pathlib import Path
import subprocess
name = %r
Path("/etc/systemd/system/" + name).write_text(%r)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "--now", name], check=True, stdout=subprocess.DEVNULL)
""" % ("saber-" + kind + "-tunnel.service", unit))
    # Capture private credentials into memory only.
    credentials = {}
    for kind, host in WORKERS.items():
        credentials[kind] = json.loads(remote(host, """
from pathlib import Path
print(Path(%r).read_text())
""" % ("/opt/agi-office-datastores/" + kind + "/credentials.json"), root=True))
    values = {
        "AGI_ES_ADDRESSES": "http://127.0.0.1:19200",
        "AGI_ES_USERNAME": credentials["elasticsearch"]["username"],
        "AGI_ES_PASSWORD": credentials["elasticsearch"]["password"],
        "AGI_NEO4J_URI": "bolt://127.0.0.1:17687",
        "AGI_NEO4J_USER": credentials["neo4j"]["username"],
        "AGI_NEO4J_PASSWORD": credentials["neo4j"]["password"],
        "AGI_KG_ENABLED": "1",
        "AGI_RUN_DB_PATH": "/var/lib/agi-office/agent_runs.sqlite3",
        "AGI_READINESS_REQUIRED": "application,run_store,milvus,elasticsearch,neo4j",
    }
    remote(CONTROL, """
from pathlib import Path
import os
os.umask(0o077)
p = Path("/etc/agi-office/datastores.env")
content = %r
if p.exists():
    previous = dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line)
    current = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
    if previous.get("AGI_ES_USERNAME") == "saber_application":
        for key in ("AGI_ES_USERNAME", "AGI_ES_PASSWORD"):
            current[key] = previous[key]
    content = chr(10).join(key + "=" + value for key, value in current.items()) + chr(10)
p.write_text(content)
p.chmod(0o600)
""" % ("\n".join(k + "=" + v for k, v in values.items()) + "\n"))
    print("Dedicated tunnels and private environment prepared; Saber not restarted.", flush=True)


if __name__ == "__main__":
    for kind, host in WORKERS.items():
        print(remote(host, WORKER_SCRIPT.replace("KIND", repr(kind), 1), root=True).strip(), flush=True)
    install_tunnels()
