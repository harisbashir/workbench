from django.test import TestCase
from django.urls import reverse

from apps.chat.formatting import render
from apps.chat.models import Channel, Message
from apps.core.models import Notification
from apps.core.testing import make_user, signed_in
from apps.projects.models import Project


class FormattingTests(TestCase):
    def test_html_is_escaped(self):
        out = render('<script>alert("x")</script> <img src=x onerror=alert(1)>')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)

    def test_markup(self):
        out = render("See PWR-12 and **this** `code` https://example.com/a?b=1 @ali")
        self.assertIn('href="/projects/PWR/tasks/12/"', out)
        self.assertIn("<strong>this</strong>", out)
        self.assertIn("<code>code</code>", out)
        self.assertIn('href="https://example.com/a?b=1"', out)
        self.assertIn('class="mention"', out)

    def test_javascript_urls_not_linked(self):
        self.assertNotIn("href=\"javascript", render("javascript:alert(1)"))


class ChannelTests(TestCase):
    def setUp(self):
        self.a = make_user("ali")
        self.b = make_user("sara")
        self.c = make_user("omar")

    def test_private_channel_hidden_from_non_members(self):
        ch = Channel.objects.create(name="secret", slug="secret", kind=Channel.Kind.PRIVATE)
        ch.members.add(self.a)
        self.assertEqual(signed_in(self.a).get(ch.get_absolute_url()).status_code, 200)
        self.assertEqual(signed_in(self.c).get(ch.get_absolute_url()).status_code, 403)
        self.assertEqual(signed_in(self.c).get(reverse("chat:poll", args=["secret"])).status_code, 403)
        self.assertEqual(signed_in(self.c).post(reverse("chat:post", args=["secret"]), {"body": "hi"}).status_code, 403)

    def test_project_channel_follows_project_membership(self):
        p = Project.objects.create(key="PWR", name="Power")
        p.members.add(self.a)
        ch = Channel.objects.create(name="pwr", slug="pwr", kind=Channel.Kind.PROJECT, project=p)
        self.assertEqual(signed_in(self.a).get(ch.get_absolute_url()).status_code, 200)
        self.assertEqual(signed_in(self.b).get(ch.get_absolute_url()).status_code, 403)

    def test_post_poll_and_unread(self):
        ch = Channel.objects.create(name="general", slug="general")
        ch.members.add(self.a, self.b)
        r = signed_in(self.a).post(reverse("chat:post", args=["general"]), {"body": "hello @sara"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertTrue(r.json()["ok"])
        self.assertTrue(Notification.objects.filter(user=self.b, text__contains="mentioned you").exists())
        cb = signed_in(self.b)
        self.assertEqual(cb.get(reverse("chat:unread")).json()["unread_total"], 1)
        data = cb.get(reverse("chat:poll", args=["general"]) + "?after=0").json()
        self.assertEqual(data["messages"][0]["author"], "sara" if False else self.a.display_name)
        self.assertEqual(data["unread_total"], 0)

    def test_only_author_can_edit(self):
        ch = Channel.objects.create(name="general", slug="general")
        m = Message.objects.create(channel=ch, author=self.a, body="typo")
        self.assertEqual(signed_in(self.b).post(reverse("chat:edit_message", args=[m.pk]), {"body": "x"}).status_code, 404)
        signed_in(self.a).post(reverse("chat:edit_message", args=[m.pk]), {"body": "fixed"})
        m.refresh_from_db()
        self.assertEqual(m.body, "fixed")

    def test_direct_message_reuses_channel(self):
        c = signed_in(self.a)
        r1 = c.post(reverse("chat:direct"), {"user": self.b.pk})
        r2 = c.post(reverse("chat:direct"), {"user": self.b.pk})
        self.assertEqual(r1["Location"], r2["Location"])
        self.assertEqual(signed_in(self.c).get(r1["Location"]).status_code, 403)

    def test_project_channel_listed_for_members(self):
        p = Project.objects.create(key="PWR", name="Power")
        p.members.add(self.a)
        Channel.objects.create(name="pwr", slug="pwr", kind=Channel.Kind.PROJECT, project=p)
        r = signed_in(self.a).get(reverse("chat:index"))
        self.assertRedirects(r, "/chat/pwr/", fetch_redirect_response=False)
        self.assertNotIn("pwr", [c.slug for c in Channel.objects.visible_to(self.b)])
