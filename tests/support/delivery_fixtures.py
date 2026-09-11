"""A fake GitHub API for delivery tests.

Records every call so a test can assert what Continuity *did*, not merely what
it returned. Deliberately a stand-in rather than the real API: `CLAUDE.md` and
the project owner both say not to create branches or pull requests just to run
tests, and a delivery test that opened a real PR on every run would be exactly
that.

The real client is exercised against live GitHub in
`tests/integration/test_external_integrations.py`, on read paths.
"""

from __future__ import annotations

from typing import Any


class FakeGitHub:
    """Implements the write surface `delivery.py` uses, and nothing else."""

    def __init__(self, *, base_sha: str = "a" * 40, number: int = 42) -> None:
        self.base_sha = base_sha
        self.number = number
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.branches: dict[str, str] = {}
        self.blobs: list[str] = []
        self.pull_requests: list[dict[str, Any]] = []

    def _record(self, call: str, **kwargs: Any) -> None:
        self.calls.append((call, kwargs))

    @property
    def called(self) -> list[str]:
        return [call for call, _ in self.calls]

    async def get_ref(self, owner: str, name: str, ref: str) -> str:
        self._record("get_ref", owner=owner, name=name, ref=ref)
        return self.base_sha

    async def create_branch(
        self, owner: str, name: str, *, branch: str, from_sha: str
    ) -> str:
        self._record("create_branch", branch=branch, from_sha=from_sha)
        self.branches[branch] = from_sha
        return from_sha

    async def create_blob(self, owner: str, name: str, *, content: str) -> str:
        self._record("create_blob", content=content)
        self.blobs.append(content)
        return f"blob{len(self.blobs)}"

    async def create_tree(
        self, owner: str, name: str, *, base_tree: str, entries: list[dict[str, Any]]
    ) -> str:
        self._record("create_tree", base_tree=base_tree, entries=entries)
        return "tree1"

    async def create_commit(
        self, owner: str, name: str, *, message: str, tree: str, parents: list[str]
    ) -> str:
        self._record("create_commit", message=message, tree=tree, parents=parents)
        return "commit1"

    async def update_branch(self, owner: str, name: str, *, branch: str, sha: str) -> None:
        self._record("update_branch", branch=branch, sha=sha)
        self.branches[branch] = sha

    async def create_pull_request(
        self, owner: str, name: str, *, title: str, body: str, head: str, base: str
    ) -> dict[str, Any]:
        self._record("create_pull_request", title=title, body=body, head=head, base=base)
        created = {
            "number": self.number,
            "html_url": f"https://github.com/{owner}/{name}/pull/{self.number}",
            "title": title,
            "body": body,
            "head": {"ref": head},
            "base": {"ref": base},
        }
        self.pull_requests.append(created)
        return created


class ExplodingGitHub(FakeGitHub):
    """Fails on the first write, to prove nothing is recorded when delivery fails."""

    async def create_branch(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("GitHub is unreachable")
