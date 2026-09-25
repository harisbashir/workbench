from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils.text import slugify


class ChannelQuerySet(models.QuerySet):
    def visible_to(self, user):
        from apps.projects.models import Project

        projects = Project.objects.visible_to(user)
        return self.filter(
            Q(kind=Channel.Kind.PUBLIC) | Q(members=user) | Q(kind=Channel.Kind.PROJECT, project__in=projects)
        ).exclude(is_archived=True).distinct()


class Channel(models.Model):
    class Kind(models.TextChoices):
        PUBLIC = "public", "Public — everyone can join"
        PRIVATE = "private", "Private — members only"
        PROJECT = "project", "Project channel"
        DIRECT = "direct", "Direct message"

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90, unique=True)
    topic = models.CharField(max_length=250, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PUBLIC)
    project = models.OneToOneField("projects.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="channel")
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="channels")
    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ChannelQuerySet.as_manager()

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("chat:channel", args=[self.slug])

    def label_for(self, user):
        if self.kind == self.Kind.DIRECT:
            others = [m.display_name for m in self.members.all() if m.pk != user.pk]
            return ", ".join(others) or "Just you"
        return f"# {self.name}"

    def can_view(self, user):
        if self.kind == self.Kind.PUBLIC:
            return True
        if self.kind == self.Kind.PROJECT and self.project_id:
            return self.project.can_view(user)
        return self.members.filter(pk=user.pk).exists()

    def can_post(self, user):
        return self.can_view(user) and not user.is_read_only and not self.is_archived

    @classmethod
    def unique_slug(cls, name):
        base = slugify(name)[:80] or "channel"
        slug, i = base, 2
        while cls.objects.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return slug

    @classmethod
    def direct_between(cls, a, b):
        existing = cls.objects.filter(kind=cls.Kind.DIRECT, members=a).filter(members=b)
        for ch in existing:
            if ch.members.count() == (1 if a == b else 2):
                return ch
        ch = cls.objects.create(name=f"dm-{a.pk}-{b.pk}", slug=cls.unique_slug(f"dm-{a.pk}-{b.pk}"),
                                kind=cls.Kind.DIRECT, created_by=a)
        ch.members.add(a, b)
        return ch


class Message(models.Model):
    class Kind(models.TextChoices):
        USER = "user", "Person"
        SYSTEM = "system", "Workbench"
        GITHUB = "github", "GitHub"

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.USER)
    body = models.TextField(max_length=8000)
    url = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    @property
    def author_name(self):
        if self.kind == self.Kind.GITHUB:
            return "GitHub"
        if self.kind == self.Kind.SYSTEM:
            return "Workbench"
        return self.author.display_name if self.author else "Former member"


class ChannelRead(models.Model):
    """Tracks the last message each person has seen, for unread counts."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE)
    last_read_id = models.BigIntegerField(default=0)

    class Meta:
        unique_together = [("user", "channel")]


def post_system_message(project, body, url="", kind=Message.Kind.SYSTEM):
    """Post an automatic message into a project's channel, if it has one."""
    channel = getattr(project, "channel", None) if project else None
    if channel is None:
        try:
            channel = Channel.objects.get(project=project)
        except Channel.DoesNotExist:
            return None
    return Message.objects.create(channel=channel, kind=kind, body=body, url=url)
