"""Bounded pre-start wait for the explicitly required remote datastores."""
import argparse
import os
import time

import requests


def check():
    required = set(os.getenv("AGI_READINESS_REQUIRED", "").split(","))
    if "elasticsearch" in required:
        addresses = [a.strip() for a in os.getenv("AGI_ES_ADDRESSES", "").split(",") if a.strip()]
        if not addresses:
            raise RuntimeError("Missing Elasticsearch endpoint")
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(addresses[0], timeout=3,
                                   auth=(os.environ["AGI_ES_USERNAME"], os.environ["AGI_ES_PASSWORD"]))
            response.raise_for_status()
    if "neo4j" in required:
        from neo4j import GraphDatabase, Query
        with GraphDatabase.driver(
            os.environ["AGI_NEO4J_URI"],
            auth=(os.environ["AGI_NEO4J_USER"], os.environ["AGI_NEO4J_PASSWORD"]),
            connection_timeout=3, connection_acquisition_timeout=3,
        ) as driver:
            with driver.session(default_access_mode="READ") as session:
                assert session.run(Query("RETURN 1 AS ok", timeout=3)).single()["ok"] == 1
                # KGStore's multihop implementation uses precisely this procedure.
                row = session.run(Query(
                    "SHOW PROCEDURES YIELD name WHERE name='apoc.path.expandConfig' RETURN count(*) AS n",
                    timeout=3,
                )).single()
                if row["n"] != 1:
                    raise RuntimeError("Required graph procedure unavailable")


def main(timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            check()
            print("Required remote datastores are ready.", flush=True)
            return 0
        except Exception as exc:
            if time.monotonic() >= deadline:
                print("Datastore pre-start deadline exceeded: " + type(exc).__name__, flush=True)
                return 1
            time.sleep(min(3, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("timeout must be 1..600 seconds")
    raise SystemExit(main(args.timeout))

