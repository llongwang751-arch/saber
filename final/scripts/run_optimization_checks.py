"""Run the full test collection in isolated processes with bounded BLAS threads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[1]
    run_dir = root / "runtime" / ("checks-" + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True)
    tests = sorted((root / "tests").glob("test_*.py"))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    rows = []
    # A few legacy API fixtures leave background workers alive until process
    # exit. Small groups keep Windows commit/handle pressure bounded.
    batch_size = 5
    for index, offset in enumerate(range(0, len(tests), batch_size), 1):
        group = tests[offset:offset+batch_size]
        xml = run_dir / f"batch-{index}.xml"
        env["AGI_EVAL_DATABASE_URL"] = "sqlite:///" + (run_dir / f"batch-{index}.db").as_posix()
        command = [sys.executable, "-m", "pytest", "-q", "--maxfail=1",
                   "--basetemp="+str(run_dir / f"tmp-{index}"), "--junitxml="+str(xml), *map(str, group)]
        with (run_dir / f"batch-{index}.log").open("w", encoding="utf8") as handle:
            try:
                process = subprocess.run(command, cwd=root, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=240)
                code = process.returncode
            except (subprocess.TimeoutExpired, OSError):
                code = -1
        row = dict(batch=index, returncode=code, files=len(group), log=str(run_dir / f"batch-{index}.log"))
        if xml.exists():
            suites = ET.parse(xml).getroot().iter("testsuite")
            counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
            for suite in suites:
                for key in counts:
                    counts[key] += int(suite.attrib.get(key, 0))
            row.update(counts)
        rows.append(row)
        print(json.dumps(row), flush=True)
        (root / "docs" / "optimization-followup-test-summary.json").write_text(
            json.dumps(dict(complete=offset+batch_size >= len(tests), passed=all(r["returncode"] == 0 for r in rows), batches=rows), indent=2), encoding="utf8")
    return int(any(r["returncode"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
