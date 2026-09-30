"""Processes GitHub webhook events.

Linking rule: any task key (e.g. PWR-12) in a pull request's title, branch
name or description, or in a commit message, links that work to the task.

Automatic task moves:
  * PR opened / marked ready for review -> linked tasks move to "In review"
  * PR merged                           -> linked tasks move to "Done"
"""
import hashlib
import hmac
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import User
from apps.chat.models import Message, post_system_message
from apps.core.utils import notify
import re

from apps.projects.models import Activity, Project, Task, log_activity

from .models import PullRequest

log = logging.getLogger("workbench")


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _user_for(login):
    if not login:
        return None
    return User.objects.filter(github_username__iexact=login, is_active=True).first()


# Task keys in any case: branch names are usually lower case ("pwr-12-usb-esd").
TASK_KEY_ANY_CASE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,9})-(\d+)\b")


def _first_line(text, limit):
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def branch_from_ref(ref):
    """'refs/heads/feature/x' -> 'feature/x'; tags and other refs -> ''."""
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ""


def _tasks_in(project, *texts):
    found = {}
    for text in texts:
        for key, num in TASK_KEY_ANY_CASE_RE.findall(text or ""):
            if key.upper() == project.key:
                t = Task.objects.filter(project=project, number=int(num)).first()
                if t:
                    found[t.pk] = t
    return list(found.values())


def _say(project, text, url=""):
    post_system_message(project, text, url, kind=Message.Kind.GITHUB)


def _act(project, text, login, task=None, url=""):
    user = _user_for(login)
    log_activity(project, text, actor=user, task=task, url=url, source=Activity.Source.GITHUB,
                 actor_label=login or "GitHub")


def handle(event, payload):
    """Returns (status, message)."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    if event == "ping":
        return "processed", "Ping received — the webhook is working."
    if event == "release":
        return on_release(repo, payload)
    projects = list(Project.objects.filter(github_repo__iexact=repo)) if repo else []
    if not projects:
        return "ignored", f"No project is linked to {repo or 'this repository'}."
    handler = HANDLERS.get(event)
    if handler is None:
        return "ignored", f"Event “{event}” isn't used by Workbench."
    for project in projects:
        handler(project, payload)
    return "processed", f"{event} for {repo}"


def on_pull_request(project, p):
    action = p.get("action")
    pr = p["pull_request"]
    login = (pr.get("user") or {}).get("login", "")
    merged = bool(pr.get("merged"))
    state = PullRequest.State.MERGED if merged else (
        PullRequest.State.CLOSED if pr.get("state") == "closed" else
        PullRequest.State.DRAFT if pr.get("draft") else PullRequest.State.OPEN)
    obj, created = PullRequest.objects.update_or_create(project=project, number=pr["number"], defaults={
        "title": pr.get("title", "")[:300], "url": pr.get("html_url", ""), "author_login": login,
        "state": state, "branch": (pr.get("head") or {}).get("ref", "")[:255],
        "head_sha": (pr.get("head") or {}).get("sha", ""),
        "opened_at": parse_datetime(pr["created_at"]) if pr.get("created_at") else timezone.now(),
    })
    tasks = _tasks_in(project, pr.get("title"), obj.branch, pr.get("body"))
    if tasks:
        obj.tasks.add(*tasks)
    tasks = list(obj.tasks.all())
    keys = ", ".join(t.key for t in tasks)
    label = f"PR #{obj.number} “{obj.title}”"
    suffix = f" (linked to {keys})" if keys else ""

    if action in ("opened", "reopened", "ready_for_review") and state == PullRequest.State.OPEN:
        _say(project, f"{login} opened {label}{suffix}", obj.url)
        for t in tasks:
            _act(project, f"opened {label} for {t.key}", login, task=t, url=obj.url)
            if t.status in (Task.Status.TODO, Task.Status.IN_PROGRESS):
                t.status = Task.Status.REVIEW
                t.save(update_fields=["status", "updated_at"])
                _act(project, f"moved {t.key} to In review (pull request opened)", login, task=t, url=obj.url)
            if t.reviewer:
                notify(t.reviewer, f"{t.key}: pull request #{obj.number} is ready for your review", obj.url)
    elif action == "opened" and state == PullRequest.State.DRAFT:
        _say(project, f"{login} opened draft {label}{suffix}", obj.url)
    elif action == "closed" and merged:
        _say(project, f"{label} was merged{suffix}", obj.url)
        for t in tasks:
            _act(project, f"merged {label}", login, task=t, url=obj.url)
            if t.status != Task.Status.DONE:
                t.status = Task.Status.DONE
                t.completed_at = timezone.now()
                t.save(update_fields=["status", "completed_at", "updated_at"])
                _act(project, f"moved {t.key} to Done (pull request merged)", login, task=t, url=obj.url)
                if t.assignee:
                    notify(t.assignee, f"{t.key} is done — PR #{obj.number} was merged", t.get_absolute_url())
    elif action == "closed":
        _say(project, f"{label} was closed without merging", obj.url)
    elif action == "synchronize":
        for t in tasks:
            _act(project, f"pushed new commits to {label}", login, task=t, url=obj.url)


def on_review(project, p):
    review = p.get("review") or {}
    pr = p.get("pull_request") or {}
    state = (review.get("state") or "").lower()
    if p.get("action") != "submitted" or state not in ("approved", "changes_requested", "commented"):
        return
    login = (review.get("user") or {}).get("login", "")
    obj = PullRequest.objects.filter(project=project, number=pr.get("number")).first()
    if obj:
        obj.review_state = state
        obj.save(update_fields=["review_state", "updated_at"])
    words = {"approved": "approved", "changes_requested": "requested changes on", "commented": "reviewed"}[state]
    url = review.get("html_url") or pr.get("html_url", "")
    _say(project, f"{login} {words} PR #{pr.get('number')} “{pr.get('title', '')}”", url)
    for t in (obj.tasks.all() if obj else []):
        _act(project, f"{words} PR #{obj.number}", login, task=t, url=url)
        if state == "changes_requested" and t.assignee:
            notify(t.assignee, f"{login} requested changes on PR #{obj.number} ({t.key})", url)
        if state == "changes_requested" and t.status == Task.Status.REVIEW:
            t.status = Task.Status.IN_PROGRESS
            t.save(update_fields=["status", "updated_at"])
            _act(project, f"moved {t.key} back to In progress (changes requested)", login, task=t, url=url)


def on_push(project, p):
    branch = branch_from_ref(p.get("ref") or "")
    if not branch:
        return  # tag pushes and other refs aren't branch work
    commits = p.get("commits") or []
    pusher = (p.get("pusher") or {}).get("name", "") or (p.get("sender") or {}).get("login", "")
    linked = 0
    for c in commits[:50]:
        msg = _first_line(c.get("message"), 200) or "(no message)"
        for t in _tasks_in(project, c.get("message")):
            _act(project, f"committed to {branch}: “{msg}”", (c.get("author") or {}).get("username") or pusher, task=t, url=c.get("url", ""))
            linked += 1
    default = (p.get("repository") or {}).get("default_branch", "main")
    if branch == default and commits:
        n = len(commits)
        _say(project, f"{pusher} pushed {n} commit{'s' if n != 1 else ''} to {branch}: “{_first_line(commits[-1].get('message'), 120) or '(no message)'}”",
             p.get("compare", ""))


def on_workflow_run(project, p):
    run = p.get("workflow_run") or {}
    if p.get("action") != "completed":
        if p.get("action") == "requested":
            PullRequest.objects.filter(project=project, head_sha=run.get("head_sha", "")).update(checks=PullRequest.Checks.PENDING)
        return
    conclusion = run.get("conclusion") or ""
    status = PullRequest.Checks.SUCCESS if conclusion == "success" else PullRequest.Checks.FAILURE if conclusion in ("failure", "timed_out", "cancelled") else ""
    prs = PullRequest.objects.filter(project=project, head_sha=run.get("head_sha", ""))
    prs.update(checks=status)
    name = run.get("name", "Workflow")
    if status == PullRequest.Checks.FAILURE:
        which = ", ".join(f"#{pr.number}" for pr in prs) or run.get("head_branch", "")
        _say(project, f"✗ {name} failed on {which}. Open the run to see the error (ERC/DRC or build).", run.get("html_url", ""))
        for pr in prs:
            for t in pr.tasks.all():
                if t.assignee:
                    notify(t.assignee, f"{name} failed on PR #{pr.number} ({t.key})", run.get("html_url", ""))
    elif status == PullRequest.Checks.SUCCESS and prs:
        _say(project, f"✓ {name} passed on " + ", ".join(f"PR #{pr.number}" for pr in prs), run.get("html_url", ""))


def on_release(repo, p):
    """A published GitHub release whose tag matches a firmware's tag prefix
    becomes a firmware release in Testing, ready for binaries and sign-off."""
    from django.db.models import Q

    from apps.firmware.models import Firmware, FirmwareRelease, parse_version

    if p.get("action") not in ("published", "prereleased", "released"):
        return "ignored", f"Release action “{p.get('action')}” isn't used."
    rel = p.get("release") or {}
    tag = rel.get("tag_name", "")
    candidates = Firmware.objects.filter(
        Q(github_repo__iexact=repo) | Q(github_repo="", project__github_repo__iexact=repo)).select_related("project")
    # Longest matching prefix wins, so "boot-v1.0.0" goes to the bootloader, not to "v…".
    matches = sorted((fw for fw in candidates if tag.startswith(fw.tag_prefix)), key=lambda fw: -len(fw.tag_prefix))
    if not matches:
        return "ignored", f"No firmware in Workbench uses tags like “{tag}” for {repo}."
    fw = matches[0]
    version = tag[len(fw.tag_prefix):]
    if parse_version(version) is None:
        return "ignored", f"Tag “{tag}” isn't a semantic version."
    obj, created = FirmwareRelease.objects.get_or_create(firmware=fw, version=version.lstrip("vV"), defaults={
        "status": FirmwareRelease.Status.TESTING, "git_ref": tag, "source_url": rel.get("html_url", "")[:200],
        "notes": (rel.get("body") or "")[:20000],
    })
    if not created:
        return "ignored", f"{fw.name} {obj.version} already exists."
    previous = fw.releases.exclude(pk=obj.pk).order_by("-sort_key").first()
    if previous:
        obj.revisions.set(previous.revisions.all())
    login = (rel.get("author") or {}).get("login", "")
    _act(fw.project, f"published {fw.name} {obj.version} on GitHub (now in Testing — upload the binaries)", login, url=obj.get_absolute_url())
    _say(fw.project, f"{login} published {fw.name} {obj.version} on GitHub. It's in Testing in Workbench — attach the binaries and sign it off.", obj.get_absolute_url())
    return "processed", f"Created {fw.name} {obj.version}"


HANDLERS = {
    "pull_request": on_pull_request,
    "pull_request_review": on_review,
    "push": on_push,
    "workflow_run": on_workflow_run,
}
