"""A project with members, a viewer and an outsider, plus a repository, for the Sources tests."""
from types import SimpleNamespace

from apps.accounts.models import User
from apps.core.testing import make_user
from apps.projects.models import Project, default_board
from apps.sources import blobs, checkin
from apps.sources.models import Repository


def files(**contents):
    """files(**{"src/main.c": "int x;"}) -> {path: Blob}"""
    return {path: blobs.store_bytes(text.encode() if isinstance(text, str) else text) for path, text in contents.items()}


def commit(repo, user, mode="update", base=None, message="Change", **contents):
    repo.refresh_from_db()
    base = repo.latest_number if base is None else base
    version, plan = checkin.commit(repo, files(**contents), mode, base, message, user, allow_partial=True)
    return version


def make_repo(kind="firmware"):
    lead = make_user("lead", role=User.Role.LEAD)
    eng = make_user("eng")
    eng2 = make_user("eng2")
    viewer = make_user("viewer", role=User.Role.VIEWER)
    outsider = make_user("out")
    p = Project.objects.create(key="PWR", name="Power", lead=lead)
    p.members.add(eng, eng2, viewer)
    board = default_board(p)
    repo = Repository.objects.create(project=p, board=board, name="Main firmware", kind=kind, created_by=eng)
    other = Project.objects.create(key="OTH", name="Other", lead=lead)
    other_repo = Repository.objects.create(project=other, name="Other code")
    return SimpleNamespace(lead=lead, eng=eng, eng2=eng2, viewer=viewer, outsider=outsider, p=p, board=board,
                           repo=repo, other=other, other_repo=other_repo)
