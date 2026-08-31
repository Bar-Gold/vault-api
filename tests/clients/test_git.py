"""The git half of the client, against a real repository on disk.

`tests/clients/test_bitbucket.py` mocks HTTP with respx because REST shapes are the thing
worth pinning there. Nothing equivalent is possible here: the contract is git's, not a JSON
body, and a fake would only prove that the fake agrees with itself — which is exactly how
the repo-scoped build-status bug survived a full suite. So these run **real git** against a
**real bare repository** in a temp directory, reached over a filesystem path instead of
HTTPS. Everything except the transport is the code that runs in production.
"""

import subprocess

import pytest
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.git import GitCommandError, GitFileRemover

PROJECT = "INFRA"
SLUG = "vault-values"
BASE_BRANCH = "master"
VALUES_PATH = "kv/myapp.yaml"
TOKEN = "s3cr3t-token"


def _git(*args, cwd):
    """Plain git, for arranging and inspecting the fixture repository."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare repo holding one values file on the base branch, plus a working clone."""
    bare = tmp_path / "scm" / PROJECT / f"{SLUG}.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", f"--initial-branch={BASE_BRANCH}", str(bare), cwd=tmp_path)

    work = tmp_path / "seed"
    work.mkdir()
    _git("init", f"--initial-branch={BASE_BRANCH}", cwd=work)
    _git("remote", "add", "origin", bare.as_posix(), cwd=work)
    values = work / "kv"
    values.mkdir()
    (values / "myapp.yaml").write_text("kvStores:\n  - name: myapp\n", encoding="utf-8")
    (values / "other.yaml").write_text("kvStores:\n  - name: other\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git(
        "-c", "user.name=seed", "-c", "user.email=seed@test", "commit", "-m", "seed",
        cwd=work,
    )
    _git("push", "origin", BASE_BRANCH, cwd=work)
    return bare


@pytest.fixture
def remover(tmp_path):
    return GitFileRemover(
        base_url=tmp_path.as_posix(),
        token=TOKEN,
        project_key=PROJECT,
        repo_slug=SLUG,
        author_name="vault-api",
        author_email="vault-api@noreply.invalid",
        timeout=60.0,
    )


def _tree(bare, ref):
    return _git("ls-tree", "-r", "--name-only", ref, cwd=bare).splitlines()


# --------------------------------------------------------------------------- #
# the removal
# --------------------------------------------------------------------------- #
async def test_remove_file_pushes_a_branch_without_the_path(origin, remover):
    """The whole reason this module exists: a commit that deletes a file."""
    commit_id = await remover.remove_file(
        path=VALUES_PATH,
        branch="vault-kv/myapp-abc123",
        base_branch=BASE_BRANCH,
        message="Delete KV store myapp",
    )

    assert VALUES_PATH not in _tree(origin, "vault-kv/myapp-abc123")
    assert _git("rev-parse", "vault-kv/myapp-abc123", cwd=origin) == commit_id


async def test_the_sibling_file_survives(origin, remover):
    """Only the named path is removed — the commit is a deletion, not a replacement."""
    await remover.remove_file(
        path=VALUES_PATH,
        branch="vault-kv/myapp-abc123",
        base_branch=BASE_BRANCH,
        message="Delete KV store myapp",
    )

    assert "kv/other.yaml" in _tree(origin, "vault-kv/myapp-abc123")


async def test_the_base_branch_is_untouched(origin, remover):
    """It pushes to the topic branch only; merging is the pull request's job."""
    before = _git("rev-parse", BASE_BRANCH, cwd=origin)

    await remover.remove_file(
        path=VALUES_PATH,
        branch="vault-kv/myapp-abc123",
        base_branch=BASE_BRANCH,
        message="Delete KV store myapp",
    )

    assert _git("rev-parse", BASE_BRANCH, cwd=origin) == before
    assert VALUES_PATH in _tree(origin, BASE_BRANCH)


async def test_the_commit_message_and_author_are_ours(origin, remover):
    await remover.remove_file(
        path=VALUES_PATH,
        branch="vault-kv/myapp-abc123",
        base_branch=BASE_BRANCH,
        message="Delete KV store myapp",
    )

    branch = "vault-kv/myapp-abc123"
    assert _git("log", "-1", "--pretty=%s", branch, cwd=origin) == "Delete KV store myapp"
    assert _git("log", "-1", "--pretty=%an", branch, cwd=origin) == "vault-api"


async def test_a_second_branch_can_be_pushed_from_the_same_base(origin, remover):
    """Each call is its own throwaway checkout; nothing is shared between them."""
    await remover.remove_file(
        path=VALUES_PATH, branch="one", base_branch=BASE_BRANCH, message="first"
    )
    await remover.remove_file(
        path="kv/other.yaml", branch="two", base_branch=BASE_BRANCH, message="second"
    )

    assert VALUES_PATH not in _tree(origin, "one")
    assert VALUES_PATH in _tree(origin, "two")


# --------------------------------------------------------------------------- #
# what a store delete leaves behind
#
# Git stores a tree of blobs and has no concept of an empty directory: a directory
# exists exactly as long as something is under it. So `git rm -r` on a store takes the
# store directory with the files, and takes the *team* directory too when that store was
# the team's last — with no code here deciding either. These pin that, because it is the
# obvious thing to "fix" by hand and because a `.gitkeep` or a per-file removal would
# quietly break it.
# --------------------------------------------------------------------------- #
@pytest.fixture
def stores(tmp_path):
    """A values repo in the real layout: one team with a single store, one with two."""
    bare = tmp_path / "scm" / PROJECT / f"{SLUG}.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", f"--initial-branch={BASE_BRANCH}", str(bare), cwd=tmp_path)

    work = tmp_path / "stores-seed"
    work.mkdir()
    _git("init", f"--initial-branch={BASE_BRANCH}", cwd=work)
    _git("remote", "add", "origin", bare.as_posix(), cwd=work)
    for team, kv in (("cloudops", "my-passwords"), ("platform", "a"), ("platform", "b")):
        directory = work / "KVs" / team / kv
        directory.mkdir(parents=True)
        (directory / "oidc-access.yaml").write_text("access: []\n", encoding="utf-8")
        (directory / "k8s-access.yaml").write_text("access: []\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git(
        "-c", "user.name=seed", "-c", "user.email=seed@test", "commit", "-m", "seed",
        cwd=work,
    )
    _git("push", "origin", BASE_BRANCH, cwd=work)
    return bare


def _dirs_under(bare, ref, prefix):
    """The directory entries git actually records at `prefix` — not derived from paths."""
    listing = _git("ls-tree", "--name-only", ref, prefix, cwd=bare)
    return listing.splitlines() if listing else []


async def test_deleting_a_store_removes_its_directory(stores, remover):
    """Both files and the directory holding them, in one `git rm -r`."""
    await remover.remove_file(
        path="KVs/cloudops/my-passwords",
        branch="drop-store",
        base_branch=BASE_BRANCH,
        message="Delete KV store my-passwords",
    )

    assert not [
        path
        for path in _tree(stores, "drop-store")
        if path.startswith("KVs/cloudops/my-passwords")
    ]


async def test_deleting_a_teams_last_store_removes_the_team_directory(stores, remover):
    """cloudops owned exactly one store, so nothing is left to keep `KVs/cloudops` alive."""
    await remover.remove_file(
        path="KVs/cloudops/my-passwords",
        branch="drop-store",
        base_branch=BASE_BRANCH,
        message="Delete KV store my-passwords",
    )

    assert _dirs_under(stores, "drop-store", "KVs/") == ["KVs/platform"]


async def test_a_team_with_another_store_keeps_its_directory(stores, remover):
    """The inverse, so the rule above cannot become "always drop the team directory"."""
    await remover.remove_file(
        path="KVs/platform/a",
        branch="drop-one-of-two",
        base_branch=BASE_BRANCH,
        message="Delete KV store a",
    )

    assert "KVs/platform" in _dirs_under(stores, "drop-one-of-two", "KVs/")
    assert "KVs/platform/b/oidc-access.yaml" in _tree(stores, "drop-one-of-two")


# --------------------------------------------------------------------------- #
# failures — every one has to arrive as ExternalServiceError, like the REST half
# --------------------------------------------------------------------------- #
async def test_removing_a_path_that_is_not_there_is_a_502(origin, remover):
    with pytest.raises(ExternalServiceError) as error:
        await remover.remove_file(
            path="kv/nope.yaml",
            branch="vault-kv/nope-abc123",
            base_branch=BASE_BRANCH,
            message="Delete KV store nope",
        )

    assert error.value.status_code == 502


async def test_an_unknown_base_branch_is_a_502(origin, remover):
    with pytest.raises(ExternalServiceError) as error:
        await remover.remove_file(
            path=VALUES_PATH,
            branch="vault-kv/myapp-abc123",
            base_branch="no-such-branch",
            message="Delete KV store myapp",
        )

    assert error.value.status_code == 502


async def test_a_missing_git_binary_is_a_502(origin, tmp_path):
    """A deployment problem — the base image may not carry git — but it must not 500."""
    remover = GitFileRemover(
        base_url=tmp_path.as_posix(),
        token=TOKEN,
        project_key=PROJECT,
        repo_slug=SLUG,
        git_binary="definitely-not-git",
    )

    with pytest.raises(ExternalServiceError) as error:
        await remover.remove_file(
            path=VALUES_PATH,
            branch="b",
            base_branch=BASE_BRANCH,
            message="m",
        )

    assert error.value.status_code == 502
    assert "git is not available" in str(error.value.detail)


async def test_the_token_never_reaches_the_error_message(origin, remover):
    """git echoes the remote in its errors; the bearer token must not ride along."""
    remover._base_url = f"https://{TOKEN}@bitbucket.invalid"

    with pytest.raises(ExternalServiceError) as error:
        await remover.remove_file(
            path=VALUES_PATH,
            branch="vault-kv/myapp-abc123",
            base_branch=BASE_BRANCH,
            message="Delete KV store myapp",
        )

    assert TOKEN not in str(error.value.detail)
    assert TOKEN not in str(error.value)


def test_the_scrubber_replaces_every_occurrence(remover):
    assert remover._scrub(f"{TOKEN} and {TOKEN}") == "*** and ***"


def test_an_empty_token_does_not_blank_the_whole_message(tmp_path):
    """`""` is in every string; scrubbing on it would erase the error."""
    remover = GitFileRemover(
        base_url=tmp_path.as_posix(), token="", project_key=PROJECT, repo_slug=SLUG
    )

    assert remover._scrub("fatal: repository not found") == (
        "fatal: repository not found"
    )


# --------------------------------------------------------------------------- #
# the pieces the caller depends on
# --------------------------------------------------------------------------- #
def test_the_clone_url_is_the_scm_path_not_the_rest_one(remover, tmp_path):
    """Bitbucket Server serves git under /scm; /rest would be the API."""
    assert remover.clone_url == f"{tmp_path.as_posix()}/scm/{PROJECT}/{SLUG}.git"


def test_a_trailing_slash_on_the_base_url_does_not_double(tmp_path):
    remover = GitFileRemover(
        base_url="https://bitbucket.test/", token=TOKEN, project_key=PROJECT, repo_slug=SLUG
    )

    assert remover.clone_url == f"https://bitbucket.test/scm/{PROJECT}/{SLUG}.git"


def test_the_token_travels_in_the_environment_not_the_command_line(remover):
    """argv is readable through `ps`; git config in the environment is not."""
    env = remover._env()

    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {TOKEN}"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    # And nothing may block on a credential prompt mid-request.
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("verify,expected", [(True, "true"), (False, "false")])
def test_ssl_verification_follows_the_setting(tmp_path, verify, expected):
    """`VERIFY_SSL` has to reach git too, or a self-signed dev host fails only here."""
    remover = GitFileRemover(
        base_url=tmp_path.as_posix(),
        token=TOKEN,
        project_key=PROJECT,
        repo_slug=SLUG,
        verify_ssl=verify,
    )

    assert remover._env()["GIT_CONFIG_VALUE_1"] == expected


def test_a_failing_command_raises_git_command_error(remover, tmp_path):
    """The internal error type, before `remove_file` maps it to ExternalServiceError."""
    with pytest.raises(GitCommandError):
        remover._run(["rev-parse", "--verify", "nope"], cwd=str(tmp_path))
