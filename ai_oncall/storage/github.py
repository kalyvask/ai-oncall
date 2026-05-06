"""GitHub HTTP client. Fetches commit metadata + patches for change-correlation
(roadmap item 4).

Single endpoint today: `GET /repos/{owner}/{repo}/commits/{sha}`. The response
exposes per-file `patch` strings; we concatenate the first few and truncate
to fit ChangeEvent.patch_excerpt (2048 chars).

Auth: bearer token via `AI_ONCALL_GITHUB_TOKEN`. Base URL via
`AI_ONCALL_GITHUB_API_URL` (defaults to https://api.github.com), so GitHub
Enterprise users can point at their own host.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

PATCH_EXCERPT_MAX = 2048
DEFAULT_API_URL = "https://api.github.com"


@dataclass(frozen=True)
class CommitPatch:
    sha: str
    title: str
    actor: str
    url: str
    patch_excerpt: str
    files_changed: list[str]


class GitHubClient:
    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        api_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if "/" not in repo:
            raise ValueError(f"github repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_commit_patch(self, sha: str) -> CommitPatch | None:
        url = f"{self.api_url}/repos/{self.repo}/commits/{sha}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self._client.get(url, headers=headers)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        files = data.get("files") or []
        patch_pieces: list[str] = []
        files_changed: list[str] = []
        for f in files:
            files_changed.append(f.get("filename", ""))
            patch = f.get("patch")
            if patch:
                patch_pieces.append(f"--- {f.get('filename', '')}\n{patch}")
        excerpt = "\n".join(patch_pieces)[:PATCH_EXCERPT_MAX]
        commit = data.get("commit") or {}
        author = commit.get("author") or {}
        return CommitPatch(
            sha=data.get("sha", sha),
            title=(commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
            actor=author.get("name") or data.get("author", {}).get("login", "unknown"),
            url=data.get("html_url", ""),
            patch_excerpt=excerpt,
            files_changed=files_changed,
        )
