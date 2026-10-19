"""The Publisher's only inputs: the owner's public sources.

  * profile/resume.md, from the Summary on (the header with phone and email is dropped);
  * job-agent-public's README.md and its git log (date and subject only);
  * profile/post_notes.md, if the owner wrote one.

Nothing else is read. This module never imports agents.db, and read_public() refuses any
path under data/, briefs/, digest/, review/, archive/ or secrets/, so job applications,
companies, recruiters and the database can't reach a prompt.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / "config" / "local.yaml"
# The public repo sits next to this one unless config/local.yaml sets public_repo.
DEFAULT_PUBLIC_REPO = Path("..") / "job-agent-public"
GIT_LOG_LIMIT = 50

PRIVATE_DIRS = ("data", "briefs", "digest", "review", "archive", "secrets", "logs")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\(?\b\d{3}\)?[ .-]?\d{3}[ .-]\d{4}\b")


class PrivatePath(ValueError):
    """A path the Publisher may never read."""


@dataclass
class Sources:
    resume: str
    readme: str
    git_log: str
    notes: str

    def as_prompt(self) -> str:
        parts = [("Resume", self.resume), ("job-agent-public README", self.readme),
                 ("job-agent-public git log (newest first)", self.git_log)]
        if self.notes:
            parts.append(("Owner's post notes", self.notes))
        return "\n\n".join(f"<source name=\"{name}\">\n{text.strip()}\n</source>" for name, text in parts if text.strip())

    def all_text(self) -> str:
        return "\n".join((self.resume, self.readme, self.git_log, self.notes))


def check_public(path: Path, root: Path = ROOT) -> Path:
    """path, resolved, unless it's inside one of the project's private folders."""
    resolved = path.resolve()
    for name in PRIVATE_DIRS:
        private = (root / name).resolve()
        if resolved == private or private in resolved.parents:
            raise PrivatePath(f"the Publisher never reads {name}/: {path}")
    return resolved


def read_public(path: Path, root: Path = ROOT) -> str:
    return check_public(path, root).read_text(encoding="utf-8")


def public_repo(config_path: Path = LOCAL_CONFIG, root: Path = ROOT) -> Path:
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    path = Path(data.get("public_repo") or DEFAULT_PUBLIC_REPO) if isinstance(data, dict) else DEFAULT_PUBLIC_REPO
    return path if path.is_absolute() else (root / path).resolve()


def resume_text(raw: str) -> str:
    """From "### Summary" on, with any email address or phone number removed."""
    start = raw.find("### Summary")
    text = raw[start:] if start >= 0 else raw
    return _PHONE.sub("", _EMAIL.sub("", text))


def git_log(repo: Path, limit: int = GIT_LOG_LIMIT) -> str:
    """"YYYY-MM-DD subject" lines: no hashes, authors or emails."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", f"-n{limit}", "--date=short", "--format=%ad %s"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {repo}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def load(root: Path = ROOT, repo: Path | None = None) -> Sources:
    repo = repo or public_repo(root=root)
    resume = resume_text(read_public(root / "profile" / "resume.md", root))
    readme_path = repo / "README.md"
    if not readme_path.is_file():
        raise FileNotFoundError(f"no README.md in {repo}; set public_repo in config/local.yaml")
    readme = readme_path.read_text(encoding="utf-8")
    notes_path = root / "profile" / "post_notes.md"
    notes = read_public(notes_path, root) if notes_path.is_file() else ""
    return Sources(resume=resume, readme=readme, git_log=git_log(repo), notes=notes)
