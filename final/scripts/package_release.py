"""Package source and built UI only; runtime data and secrets never enter a release."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile


def package(root, output, frontend_dir=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    frontend = Path(frontend_dir).resolve() if frontend_dir else root / "web/dist"
    files = set()
    for directory in ("internal", "config", "alembic"):
        files.update(p for p in (root / directory).rglob("*.py") if "__pycache__" not in p.parts)
    files.update(root / p for p in ("main.py", "alembic.ini", "scripts/backup_server.py"))
    if not (frontend / "index.html").is_file():
        raise FileNotFoundError("Build web/dist before packaging")
    sources = {p.relative_to(root).as_posix(): p for p in files}
    sources.update({"web/dist/" + p.relative_to(frontend).as_posix(): p
                    for p in frontend.rglob("*") if p.is_file()})
    manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in sorted(sources.items())}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in manifest:
            archive.add(sources[name], arcname=name, recursive=False)
        data = json.dumps({"schema": 1, "files": manifest}, sort_keys=True).encode()
        info = tarfile.TarInfo("release-manifest.json")
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))
    return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": len(manifest)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frontend-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.output, args.frontend_dir)))
