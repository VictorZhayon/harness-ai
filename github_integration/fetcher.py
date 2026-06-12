"""File fetching from a GitHub repository.

# GITHUB INTEGRATION: File Fetcher
Pulls raw file contents from the repo's default branch. Fetching happens once
per run (step 2 of the harness flow); the agent's tools then operate on the
in-memory copies and never hit GitHub again during the run.
"""

from github import GithubException

from github_integration.client import GitHubIntegrationError, get_repo

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build"}

DEFAULT_EXTENSIONS = (".py", ".ts", ".js")


def fetch_files(repo_full_name: str, file_paths: list[str]) -> dict[str, str]:
    """Fetch each path from the repo's default branch.

    Returns {file_path: file_content}. Raises GitHubIntegrationError (422) if
    any requested path is missing — a partial fetch would let the agent
    silently document less than the user asked for.
    """
    repo = get_repo(repo_full_name)
    ref = repo.default_branch

    fetched: dict[str, str] = {}
    missing: list[str] = []
    for path in file_paths:
        try:
            blob = repo.get_contents(path, ref=ref)
        except GithubException:
            missing.append(path)
            continue
        if isinstance(blob, list):  # path was a directory, not a file
            missing.append(path)
            continue
        fetched[path] = blob.decoded_content.decode("utf-8", errors="replace")

    if missing:
        raise GitHubIntegrationError(
            f"Files not found on '{ref}' of {repo_full_name}: {missing}", status_code=422
        )
    return fetched


def crawl_repo_tree(
    repo_full_name: str, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
) -> list[str]:
    """Recursively list all source files in the repo matching the extensions,
    skipping dependency/build/VCS directories."""
    repo = get_repo(repo_full_name)
    try:
        branch = repo.get_branch(repo.default_branch)
        tree = repo.get_git_tree(branch.commit.sha, recursive=True)
    except GithubException as exc:
        raise GitHubIntegrationError(
            f"Could not read tree of {repo_full_name}: {exc}", status_code=422
        ) from exc

    paths: list[str] = []
    for element in tree.tree:
        if element.type != "blob":
            continue
        parts = element.path.split("/")
        if any(part in SKIP_DIRS for part in parts):
            continue
        if any(element.path.endswith(ext) for ext in extensions):
            paths.append(element.path)
    return paths
