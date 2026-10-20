"""Scoped Pi theme release. No app imports, database access, or service restarts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path("/home/kkk777/SmartMiniDairyDashboard")
PAGES = [
    "templates/" + name + ".html"
    for name in ("index", "admin", "login", "cow_detail", "milk_yield", "inventory_history")
]
ASSETS = [
    "static/degen-theme.20261020.css",
    "static/degen-theme.20261020.js",
    "templates/partials/dairy_theme.html",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        output.write(content)
        temporary = Path(output.name)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    package = args.package.resolve()
    if not ROOT.is_dir() or ROOT.resolve() != ROOT:
        raise RuntimeError("Unexpected deployment root")
    manifest = json.loads((package / "manifest.json").read_text())
    if set(manifest["baseline"]) != set(PAGES):
        raise RuntimeError("Invalid baseline allowlist")
    if set(manifest["release"]) != set(PAGES + ASSETS):
        raise RuntimeError("Invalid release allowlist")

    originals = {}
    payloads = {}
    for name in ASSETS + PAGES:
        target = (ROOT / name).resolve()
        target.relative_to(ROOT)  # Refuse symlinks outside this application.
        data = (package / name).read_bytes()
        if digest(data) != manifest["release"][name]:
            raise RuntimeError(f"Package checksum mismatch: {name}")
        payloads[name] = data
        if name in PAGES:
            current = target.read_bytes()
            if digest(current) != manifest["baseline"][name]:
                raise RuntimeError(f"Another agent changed {name}; deployment stopped")
            originals[name] = current
        elif target.exists() and target.read_bytes() != data:
            raise RuntimeError(f"Will not overwrite existing versioned asset: {name}")

    # Parse templates without importing app.py (its startup changes the database).
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(package / "templates"))
    for name in PAGES + [ASSETS[-1]]:
        env.parse(payloads[name].decode("utf-8"))

    backup_dir = ROOT / "theme_backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"before-degen-theme-{time.time_ns()}.tar.gz"
    with tarfile.open(backup, "x:gz") as archive:
        for name in PAGES:
            archive.add(ROOT / name, arcname=name)

    changed = []
    try:
        # Publish assets first, so new templates never point at missing files.
        for name in ASSETS + PAGES:
            target = ROOT / name
            if name in PAGES and target.read_bytes() != originals[name]:
                raise RuntimeError(f"Concurrent edit: {name}")
            atomic_write(target, payloads[name])
            changed.append(name)
        request = urllib.request.Request(
            "http://127.0.0.1/smartdairy/login",
            headers={"Host": "degenpredictions.com"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read().decode()
            if response.status != 200 or "degen-theme.20261020.css" not in html:
                raise RuntimeError("Updated login theme not served")
    except Exception:
        for name in changed:
            if name in originals and (ROOT / name).read_bytes() == payloads[name]:
                atomic_write(ROOT / name, originals[name])
        # Newly published, unreferenced assets are harmless; retain them for audit.
        raise
    print(json.dumps({
        "status": "deployed", "files": len(changed), "backup": str(backup),
        "database_changed": False, "services_restarted": False,
    }))


if __name__ == "__main__":
    main()
