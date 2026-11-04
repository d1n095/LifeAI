"""CI / test harness for Level-2 repository-writing tests.

Four production-flow tests create throwaway Git repos and run `git commit -qm ...`
without setting a repo-local identity. GitHub-hosted runners have no user.name /
user.email, so those commits fail with `Author identity unknown`.

This package-scoped harness sets a deterministic, non-personal identity via the
Git environment variables (which override local/global gitconfig). It does not
change application authority, production code, or the developer's gitconfig.
"""

from __future__ import annotations

import os


# .test TLD — not a person, not a production mailbox.
_CI_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "lifeai-ci",
    "GIT_AUTHOR_EMAIL": "ci@lifeai.test",
    "GIT_COMMITTER_NAME": "lifeai-ci",
    "GIT_COMMITTER_EMAIL": "ci@lifeai.test",
}


def pytest_configure() -> None:
    for key, value in _CI_GIT_IDENTITY.items():
        os.environ[key] = value
