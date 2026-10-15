"""Run the real-socket test suite and write a portable JSON verification record."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_manifest():
    files = []
    for folder in ("Hotel", "Talk", "tests", "scripts"):
        files.extend((ROOT / folder).rglob("*.py"))
    files.extend(
        ROOT / name
        for name in (
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-lock.txt",
            "requirements-dev-lock.txt",
            "Hotel/api.ini",
        )
    )
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(
            p.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for p in sorted(files)
        if p.is_file()
    }


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active = None

    def startTest(self, test):
        super().startTest(test)
        self.active = {
            "id": test.id(),
            "status": "running",
            "started": time.perf_counter(),
        }

    def addSuccess(self, test):
        super().addSuccess(test)
        self.active["status"] = "passed"

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.active["status"] = "failed"

    def addError(self, test, error):
        super().addError(test, error)
        self.active["status"] = "error"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.active["status"] = "skipped"
        self.active["reason"] = reason

    def stopTest(self, test):
        self.active["seconds"] = round(
            time.perf_counter() - self.active.pop("started"), 6
        )
        self.records.append(self.active)
        self.active = None
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "target/verification.json"
    )
    args = parser.parse_args()
    before = source_manifest()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult)
    started = time.perf_counter()
    result = runner.run(suite)
    after = source_manifest()
    packages = {}
    for name in (
        "requests",
        "urllib3",
        "charset-normalizer",
        "idna",
        "certifi",
        "ruff",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    report = {
        "schema": "network-systems-verification/v1",
        "environment": {
            "python": platform.python_version(),
            "os": platform.system(),
            "machine": platform.machine(),
            "packages": packages,
        },
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.perf_counter() - started, 6),
        "successful": result.wasSuccessful() and before == after,
        "source_changed_during_tests": before != after,
        "source_hash_policy": "SHA-256 of UTF-8 source with CRLF normalized to LF, ignoring checkout line endings.",
        "source_sha256": before,
        "checks": result.records,
        "scope": "Temporary local TCP/HTTP servers, temporary SQLite databases, controlled faults, real CLI subprocesses. No external APIs.",
        "process_id_not_published": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if report["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
