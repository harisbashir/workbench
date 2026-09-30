"""Regression tests for the pre-launch review of chat."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.chat.formatting import render
from apps.chat.models import Channel, ChannelRead, Message
from apps.chat.views import unread_counts
from apps.core.testing import make_user, signed_in
from apps.projects.models import Project


class FormattingTests(TestCase):
    def test_placeholder_characters_in_input_do_not_crash(self):
        for text in ("a \x017\x01 b", "x \x003\x00", "`c` \x0199\x01", "```f``` \x005\x00"):
            html = str(render(text))
            self.assertNotIn("\x00", html)
            self.assertNotIn("\x01", html)

    def test_formatting_still_works(self):
        html = str(render("**b** `c` ```\nfence``` PWR-1 @sana"))
        self.assertIn("<strong>b</strong>", html)
        self.assertIn("<code>c</code>", html)
        self.assertIn("<pre><code>fence</code></pre>", html)
        self.assertIn('href="/projects/PWR/tasks/1/"', html)


class ChatViewTests(TestCase):
    def setUp(self):
        self.eng = make_user("eng")
        self.other = make_user("other")
        self.viewer = make_user("view", role=User.Role.VIEWER)
        self.project = Project.objects.create(key="PWR", name="Power")
        self.project.members.add(self.eng, self.other, self.viewer)
        self.ch = Channel.objects.create(name="pwr", slug="pwr", kind="project", project=self.project)
        self.c = signed_in(self.eng)

    def post(self, body, slug="pwr"):
        return self.c.post(reverse("chat:post", args=[slug]), {"body": body}, HTTP_X_REQUESTED_WITH="fetch")

    def test_control_characters_are_stripped_and_channel_still_opens(self):
        self.post("hi \x017\x01 \x00there")
        self.assertEqual(Message.objects.get().body, "hi 7 there")
        self.assertEqual(self.c.get(reverse("chat:channel", args=["pwr"])).status_code, 200)

    def test_poll_with_bad_numbers_is_not_500(self):
        for q in ("after=abc", "before=x", "after=-5", "since=nonsense"):
            self.assertEqual(self.c.get(reverse("chat:poll", args=["pwr"]) + "?" + q).status_code, 200, q)

    def test_edit_limits(self):
        msg = Message.objects.create(channel=self.ch, author=self.eng, body="hello")
        url = reverse("chat:edit_message", args=[msg.pk])
        self.assertEqual(self.c.post(url, {"body": "x" * 8001}).status_code, 400)
        self.assertEqual(self.c.post(url, {"body": "ok \x00\x01"}).status_code, 200)
        msg.refresh_from_db()
        self.assertEqual(msg.body, "ok")

    def test_cannot_edit_in_archived_channel_or_after_losing_access(self):
        msg = Message.objects.create(channel=self.ch, author=self.eng, body="hello")
        url = reverse("chat:edit_message", args=[msg.pk])
        self.ch.is_archived = True
        self.ch.save()
        self.assertEqual(self.c.post(url, {"body": "changed"}).status_code, 403)
        self.ch.is_archived = False
        self.ch.save()
        self.project.members.remove(self.eng)
        self.assertEqual(self.c.post(url, {"action": "delete"}).status_code, 403)

    def test_read_only_user_cannot_edit_old_messages(self):
        msg = Message.objects.create(channel=self.ch, author=self.viewer, body="old")
        r = signed_in(self.viewer).post(reverse("chat:edit_message", args=[msg.pk]), {"body": "new"})
        self.assertEqual(r.status_code, 403)

    def test_reserved_names_get_usable_slugs(self):
        for name in ("new", "unread", "direct", "message"):
            self.c.post(reverse("chat:create"), {"name": name, "kind": "public"})
            ch = Channel.objects.get(name=name)
            self.assertNotEqual(ch.slug, name)
            r = self.c.get(ch.get_absolute_url())
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.context["channel"], ch)

    def test_unread_counts(self):
        Message.objects.create(channel=self.ch, author=self.other, body="1")
        Message.objects.create(channel=self.ch, author=self.other, body="2")
        Message.objects.create(channel=self.ch, author=self.eng, body="mine")
        Message.objects.create(channel=self.ch, kind=Message.Kind.SYSTEM, body="bot")
        Message.objects.create(channel=self.ch, author=self.other, body="gone", is_deleted=True)
        self.assertEqual(unread_counts(self.eng), {self.ch.pk: 3})
        ChannelRead.objects.create(user=self.eng, channel=self.ch, last_read_id=Message.objects.get(body="1").pk)
        self.assertEqual(unread_counts(self.eng), {self.ch.pk: 2})
        # Private channels count only for members; other projects not at all.
        private = Channel.objects.create(name="secret", slug="secret", kind="private")
        Message.objects.create(channel=private, author=self.other, body="psst")
        hidden = Channel.objects.create(name="x", slug="x", kind="project", project=Project.objects.create(key="HID", name="h"))
        Message.objects.create(channel=hidden, author=self.other, body="nope")
        self.assertEqual(unread_counts(self.eng), {self.ch.pk: 2})
        private.members.add(self.eng)
        self.assertEqual(unread_counts(self.eng), {self.ch.pk: 2, private.pk: 1})

    def test_unread_is_constant_queries(self):
        lead = make_user("lead", role=User.Role.LEAD)
        with CaptureQueriesContext(connection) as ctx:
            unread_counts(lead)
        few = len(ctx.captured_queries)
        for i in range(10):
            p = Project.objects.create(key=f"P{i}", name=f"p{i}")
            ch = Channel.objects.create(name=f"p{i}", slug=f"p{i}", kind="project", project=p)
            Message.objects.create(channel=ch, author=self.other, body="hi")
        with CaptureQueriesContext(connection) as ctx:
            counts = unread_counts(lead)
        self.assertEqual(len(ctx.captured_queries), few)
        self.assertEqual(len(counts), 10)

    def test_chat_uploads_with_same_name_are_separate_documents(self):
        from apps.files.models import Document
        url = reverse("chat:upload", args=["pwr"])
        self.c.post(url, {"files": SimpleUploadedFile("photo.png", b"one")})
        signed_in(self.other).post(url, {"files": SimpleUploadedFile("photo.png", b"two")})
        docs = Document.objects.order_by("pk")
        self.assertEqual([d.name for d in docs], ["photo.png", "photo (2).png"])
        self.assertEqual([d.version_count for d in docs], [1, 1])
        urls = list(Message.objects.order_by("pk").values_list("url", flat=True))
        self.assertEqual(urls, [d.get_absolute_url() for d in docs])
