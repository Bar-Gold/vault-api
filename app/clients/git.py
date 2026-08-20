"""Git-over-HTTPS, for the one thing Bitbucket Server's REST API cannot do: delete a file.

**Why this module exists at all.** Every other repo operation in this service is REST
(`app/clients/bitbucket.py`). File *deletion* has no REST equivalent on Server/Data Center
— the `browse/{path}` resource exposes `GET` and `PUT` and nothing else, and Atlassian have
said outright that deleting files over REST is not supported. The workaround every search
result offers (`-F files=<path>` against `/repositories/{workspace}/{slug}/src`) is the
**Bitbucket Cloud** API; that resource does not exist on Server, so it 404s here. There is
no keyed variant and no undocumented verb — the only way to author a commit that removes a
path is to speak git.

**Why HTTPS and not SSH.** A push needs credentials either way, and the service already
holds a Bitbucket HTTP access token for REST. DC accepts that same token for git over
HTTPS as a bearer header, so this needs no keypair, no `known_hosts`, no second secret and
no rotation story of its own. The token goes in through `GIT_CONFIG_*` environment
variables rather than `-c http.extraHeader=...` on the command line, because argv is world
readable through `ps` on a shared host.

**What it deliberately does not do.** It does not open, merge or gate a pull request, and
it does not push to the base branch. It produces exactly one commit on one short-lived
branch and stops, so the pull-request spine in `operations.py` stays in charge of every
decision that matters — the same branch a `put_file` would have produced, arrived at
differently. Rollback is unchanged too: the branch is deleted over REST like any other.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List

from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

SERVICE_NAME = "bitbucket"

# Bitbucket Server serves git over HTTPS under /scm, not under /rest.
GIT_HTTP_PREFIX = "scm"


class GitCommandError(Exception):
    """A git invocation exited non-zero. Carries stderr with the token already scrubbed."""


class GitFileRemover:
    """Removes one path on one branch, by pushing a commit over HTTPS.

    Constructed once in `main.py` and handed to `BitbucketClient`, which exposes it as
    `delete_file` so `operations.py` still talks to a single upstream object.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        project_key: str,
        repo_slug: str,
        verify_ssl: bool = True,
        author_name: str = "vault-api",
        author_email: str = "vault-api@noreply.invalid",
        timeout: float = 120.0,
        git_binary: str = "git",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._project_key = project_key
        self._repo_slug = repo_slug
        self._verify_ssl = verify_ssl
        self._author_name = author_name
        self._author_email = author_email
        self._timeout = timeout
        self._git = git_binary

    @property
    def clone_url(self) -> str:
        return (
            f"{self._base_url}/{GIT_HTTP_PREFIX}/"
            f"{self._project_key}/{self._repo_slug}.git"
        )

    def _scrub(self, text: str) -> str:
        """Keep the token out of logs and error bodies, wherever git echoed it."""
        if self._token and self._token in text:
            text = text.replace(self._token, "***")
        return text

    def _env(self) -> Dict[str, str]:
        """Auth and TLS as git config in the environment, never on the command line."""
        env = dict(os.environ)
        env.update(
            {
                # Never block on a credential prompt: fail fast instead of hanging a
                # request until the timeout.
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {self._token}",
                "GIT_CONFIG_KEY_1": "http.sslVerify",
                "GIT_CONFIG_VALUE_1": "true" if self._verify_ssl else "false",
            }
        )
        return env

    def _run(self, args: List[str], cwd: str) -> str:
        completed = subprocess.run(
            [self._git, *args],
            cwd=cwd,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if completed.returncode != 0:
            detail = self._scrub(
                (completed.stderr or completed.stdout or "").strip()
            ) or f"git {args[0]} exited {completed.returncode}"
            raise GitCommandError(f"git {args[0]}: {detail}")
        return completed.stdout.strip()

    def _remove_file_sync(
        self, path: str, branch: str, base_branch: str, message: str
    ) -> str:
        """The blocking half: a throwaway checkout, one commit, one push.

        `--depth 1` on a single branch keeps this to one commit's worth of tree however
        large the repo's history is. A deletion needs the path's *name*, not its bytes, but
        a blobless fetch is not worth the server-support question for one small file.
        """
        workspace = tempfile.mkdtemp(prefix="vault-api-git-")
        try:
            self._run(["init", "--quiet"], cwd=workspace)
            self._run(["remote", "add", "origin", self.clone_url], cwd=workspace)
            self._run(
                ["fetch", "--quiet", "--depth", "1", "origin", base_branch],
                cwd=workspace,
            )
            self._run(["checkout", "--quiet", "-B", branch, "FETCH_HEAD"], cwd=workspace)
            self._run(["rm", "--quiet", "--", path], cwd=workspace)
            self._run(
                [
                    "-c",
                    f"user.name={self._author_name}",
                    "-c",
                    f"user.email={self._author_email}",
                    "commit",
                    "--quiet",
                    "--message",
                    message,
                ],
                cwd=workspace,
            )
            # An explicit refspec: `git push origin <branch>` resolves the source ref
            # locally and the destination by config, and this branch may or may not exist
            # remotely depending on whether the caller created it over REST first.
            self._run(
                ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=workspace
            )
            return self._run(["rev-parse", "HEAD"], cwd=workspace)
        finally:
            # Windows leaves .git objects read-only, which a plain rmtree refuses.
            shutil.rmtree(workspace, ignore_errors=True)

    async def remove_file(
        self, path: str, branch: str, base_branch: str, message: str
    ) -> str:
        """Commit the removal of `path` onto `branch`, and return the new commit's sha.

        Runs the blocking git work in a worker thread: this service is async all the way
        down, and a subprocess plus a network round-trip on the event loop would stall
        every other in-flight request for the length of a fetch and a push.

        Every failure leaves as `ExternalServiceError`, the same type `http.py` raises, so
        the routes map it without knowing a subprocess was involved.
        """
        logger.info(f"Removing {path} on {branch} via git")
        try:
            commit_id = await asyncio.to_thread(
                self._remove_file_sync, path, branch, base_branch, message
            )
        except subprocess.TimeoutExpired as timed_out:
            # 504 rather than 502, for the same reason `http.py` maps a read timeout that
            # way: `routes.py` keys off the status to answer 504 instead of 502.
            raise ExternalServiceError(
                SERVICE_NAME,
                504,
                detail=f"git timed out removing {path} after {self._timeout}s",
            ) from timed_out
        except FileNotFoundError as missing_git:
            # The binary itself is absent — a deployment problem rather than an upstream
            # one, but it still has to surface as something the routes can map.
            raise ExternalServiceError(
                SERVICE_NAME,
                502,
                detail=f"git is not available on this host: {missing_git}",
            ) from missing_git
        except GitCommandError as failed:
            raise ExternalServiceError(SERVICE_NAME, 502, detail=str(failed)) from failed

        logger.info(f"Removed {path} on {branch} as {commit_id}")
        return commit_id
