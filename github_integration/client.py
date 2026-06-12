"""GitHub authentication and repo access.

# GITHUB INTEGRATION: Client
The single place a PyGithub client is constructed. Every other module gets a
repo through get_repo(); nothing outside github_integration/ talks to GitHub.

The PAT only needs `repo` and `pull_requests` scopes.
"""

import os
from functools import lru_cache

from github import Auth, Github
from github.Repository import Repository


class GitHubIntegrationError(Exception):
    """Raised when a GitHub operation fails. Carries an HTTP status hint so
    the API layer can map fetch problems (client error) vs. write problems
    (upstream error) without importing PyGithub itself."""

    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


@lru_cache(maxsize=1)
def _client() -> Github:
    token = os.environ.get("GITHUB_PAT")
    if not token:
        raise GitHubIntegrationError("GITHUB_PAT is not set", status_code=500)
    return Github(auth=Auth.Token(token))


def get_repo(repo_full_name: str) -> Repository:
    """Resolve 'owner/repo-name' to a PyGithub Repository."""
    try:
        return _client().get_repo(repo_full_name)
    except Exception as exc:
        raise GitHubIntegrationError(
            f"Could not access repo '{repo_full_name}': {exc}", status_code=422
        ) from exc
