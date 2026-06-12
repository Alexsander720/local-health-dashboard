from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PRIVATE_FILES = {
    "manual_notes.json",
    "body_measurements.json",
    "food_profile.json",
    "food_profile.md",
    "yazio_notes_archive.json",
    "mi_cloud.json",
    "mi_cookies.json",
    "gemini_key.txt",
    "gemini_project.txt",
    "latest_sync.json",
    "dashboard.html",
}

SECRET_PATTERNS = {
    "google-api-key": re.compile("AI" + r"za[0-9A-Za-z_-]{20,}"),
    "openai-api-key": re.compile(r"sk-(?:proj-)?[0-9A-Za-z_-]{20,}"),
    "github-token": re.compile(r"gh[opusr]_[0-9A-Za-z]{20,}"),
    "private-key": re.compile(
        "-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "personal-windows-path": re.compile(
        r"(?:C:\\Users\\ban13|E:\\Scripts)", re.IGNORECASE
    ),
}

BINARY_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line) for line in result.stdout.splitlines() if line]


def scan_paths(root: Path, paths: list[Path]) -> list[dict]:
    findings = []
    for relative in paths:
        if relative.name in PRIVATE_FILES:
            findings.append(
                {
                    "kind": "private-file",
                    "path": relative.as_posix(),
                    "line": None,
                }
            )

        path = root / relative
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    {
                        "kind": kind,
                        "path": relative.as_posix(),
                        "line": line,
                        "excerpt": lines[line - 1][:160] if lines else "",
                    }
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject private files and high-confidence secrets before publication."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_paths(root, tracked_paths(root))
    if findings:
        for finding in findings:
            location = finding["path"]
            if finding.get("line"):
                location += f":{finding['line']}"
            print(f"{finding['kind']}: {location}")
        return 1
    print("Public release audit passed: no tracked private files or high-confidence secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
