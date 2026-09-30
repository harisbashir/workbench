"""Validation of drawings from the browser, safe rendering, fast layout and the review rules."""
import json
import time

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.core.testing import make_user, signed_in
from apps.diagrams import geometry, render, starters
from apps.diagrams.models import Diagram, DiagramVersion
from apps.projects.models import Project


def _old_wrap(text, width, size, bold=False):
    """The original (slow) algorithm, to check the fast one gives the same lines."""
    lines = []
    for para in (text or "").split("\n"):
        line = ""
        for w in para.split(" "):
            cand = w if not line else line + " " + w
            if geometry.text_width(cand, size, bold) <= width:
                line = cand
                continue
            if line:
                lines.append(line)
            line = ""
            while geometry.text_width(w, size, bold) > width and len(w) > 1:
                k = len(w)
                while k > 1 and geometry.text_width(w[:k], size, bold) > width:
                    k -= 1
                lines.append(w[:k])
                w = w[k:]
            line = w
        lines.append(line)
    while lines and lines[-1] == "" and len(lines) > 1:
        lines.pop()
    return lines


def node(i, **kw):
    return {"id": f"n{i}", "type": "block", "x": i * 150, "y": 0, "w": 140, "h": 60, "label": f"B{i}", **kw}


class GeometryTests(SimpleTestCase):
    def test_wrap_matches_the_original_algorithm(self):
        texts = ["", "a", "Power board — with a long name that wraps onto two lines", "x" * 90, "a  b   c", " lead",
                 "trail ", "WWWWWWWWWWWWWWWWWWWWWWWW tiny WWWWWWWWWWW", "line one\nline two\n\n", "Ω → 10 kΩ ±1 %", "iiii" * 30]
        for t in texts:
            for width in (20, 57.5, 124, 300):
                for bold in (False, True):
                    self.assertEqual(geometry.wrap(t, width, 13, bold), _old_wrap(t, width, 13, bold), (t, width, bold))

    def test_wrap_stops_at_max_lines(self):
        self.assertEqual(len(geometry.wrap("word " * 500, 40, 13, max_lines=3)), 3)

    def test_layout_of_huge_texts_is_fast(self):
        nodes = [{"id": f"n{i}", "type": "block", "x": i * 30, "y": 0, "w": 20, "h": 16, "label": "W" * 200, "sub": "W" * 300}
                 for i in range(geometry.MAX_NODES)]
        data = geometry.clean({"nodes": nodes})
        t = time.monotonic()
        render.to_svg(data)
        render.to_pdf(data)
        self.assertLess(time.monotonic() - t, 5)

    def test_clean_limits_and_types(self):
        bad = [[], {"nodes": 5}, {"nodes": {}}, {"nodes": [5]}, {"nodes": [node(1, id='a" onmouseover="x')]},
               {"nodes": [node(1), node(1)]}, {"nodes": [node(1, x="nan")]}, {"nodes": [node(1, x=[1])]},
               {"nodes": [node(i) for i in range(geometry.MAX_NODES + 1)]},
               {"nodes": [node(1), node(2)], "edges": [{"from": "n1", "to": "n2"}] * (geometry.MAX_EDGES + 1)}]
        for d in bad:
            with self.assertRaises(ValueError, msg=str(d)[:80]):
                geometry.clean(d)

    def test_clean_drops_bad_edges_and_fixes_ids(self):
        d = geometry.clean({"nodes": [node(1), node(2, type=["x"], color={"a": 1})],
                            "edges": [{"from": ["n1"], "to": "n2"}, {"id": "e1", "from": "n1", "to": "n2"},
                                      {"id": "e1", "from": "n2", "to": "n1", "kind": ["bus"]}]})
        self.assertEqual([e["id"] for e in d["edges"]], ["e1", "e2"])
        self.assertEqual(d["nodes"][1]["type"], "block")
        self.assertNotIn("color", d["nodes"][1])

    def test_control_characters_are_removed(self):
        d = geometry.clean({"nodes": [node(1, label="a\x01b\x7f\ud800c\r\nd")]})
        self.assertEqual(d["nodes"][0]["label"], "abc\nd")

    def test_svg_attributes_are_escaped(self):
        data = {"nodes": [dict(node(1), id='a" onmouseover="alert(1)', label='<script>"x"</script>')], "edges": []}
        svg = render.to_svg(data, interactive=True)  # old stored data that was never cleaned
        self.assertNotIn('onmouseover="', svg)
        self.assertIn('data-node="a&quot; onmouseover=&quot;alert(1)"', svg)
        self.assertNotIn("<script", svg)


class DiagramViewTests(TestCase):
    def setUp(self):
        self.lead = make_user("lead", role=User.Role.LEAD)
        self.eng = make_user("eng")
        self.viewer = make_user("viewer", role=User.Role.VIEWER)
        self.other_lead = make_user("lead2", role=User.Role.LEAD)
        self.project = Project.objects.create(key="DG", name="Diagrams", lead=self.lead)
        self.project.members.add(self.eng, self.viewer)
        self.d = Diagram.objects.create(project=self.project, name="System", level="system", created_by=self.eng, current_version=1)
        DiagramVersion.objects.create(diagram=self.d, number=1, data=geometry.clean(starters.blank()), created_by=self.eng)
        self.save_url = reverse("diagrams:save", args=[self.project.key, self.d.pk])
        self.status_url = reverse("diagrams:status", args=[self.project.key, self.d.pk])

    def post_json(self, client, url, body):
        return client.post(url, body if isinstance(body, (bytes, str)) else json.dumps(body), content_type="application/json")

    def test_save_rejects_malformed_json_with_400(self):
        c = signed_in(self.eng)
        for body in (b"not json", "[1, 2]", {"data": {"nodes": 5}}, {"data": {"nodes": []}, "base": "abc"},
                     {"data": {"nodes": [node(1, id="bad id!")]}}, b"\xff\xfe"):
            r = self.post_json(c, self.save_url, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertFalse(r.json()["ok"])

    def test_save_rejects_huge_payload(self):
        body = {"data": {"nodes": [node(1, notes="x" * 2000)]}, "pad": "x" * (geometry.MAX_JSON_BYTES + 1)}
        r = self.post_json(signed_in(self.eng), self.save_url, body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("too large", r.json()["error"])

    def test_save_and_viewers(self):
        body = {"data": {"nodes": [node(1)], "edges": []}, "base": 1}
        self.assertEqual(self.post_json(signed_in(self.viewer), self.save_url, body).status_code, 403)
        r = self.post_json(signed_in(self.eng), self.save_url, body)
        self.assertEqual(r.json()["version"], 2)

    def test_preview_pdf_needs_edit_permission(self):
        url = reverse("diagrams:preview_pdf", args=[self.project.key, self.d.pk])
        body = {"data": {"nodes": [node(1)]}, "page": ["A4"]}
        self.assertEqual(self.post_json(signed_in(self.viewer), url, body).status_code, 403)
        r = self.post_json(signed_in(self.eng), url, body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_detail_page_escapes_node_ids(self):
        v = self.d.versions.get()
        v.data = {"v": 1, "nodes": [dict(node(1), id='a" data-x="1')], "edges": [], "page": {}}
        v.save()
        html = signed_in(self.eng).get(self.d.get_absolute_url()).content.decode()
        self.assertNotIn('data-x="1"', html)

    def submit(self):
        Diagram.objects.filter(pk=self.d.pk).update(status=Diagram.Status.REVIEW)
        self.d.refresh_from_db()

    def test_approval_needs_review_and_the_reviewed_version(self):
        c = signed_in(self.lead)
        c.post(self.status_url, {"action": "approve", "version": 1})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, "draft", "a draft can't be approved without review")
        self.submit()
        # the author saves v2 while the lead is looking at v1
        self.post_json(signed_in(self.eng), self.save_url, {"data": {"nodes": [node(1)]}, "base": 1})
        c.post(self.status_url, {"action": "approve", "version": 1})
        self.d.refresh_from_db()
        self.assertEqual((self.d.status, self.d.approved_version), ("review", None))
        c.post(self.status_url, {"action": "approve", "version": 2})
        self.d.refresh_from_db()
        self.assertEqual((self.d.status, self.d.approved_version), ("approved", 2))

    def test_lead_cant_approve_own_version_when_someone_else_can(self):
        self.project.members.add(self.other_lead)
        self.post_json(signed_in(self.lead), self.save_url, {"data": {"nodes": [node(1)]}, "base": 1})
        self.submit()
        page = signed_in(self.lead).get(self.d.get_absolute_url()).content.decode()
        self.assertIn("another lead or administrator", page)
        signed_in(self.lead).post(self.status_url, {"action": "approve", "version": 2})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, "review")
        signed_in(self.other_lead).post(self.status_url, {"action": "approve", "version": 2})
        self.d.refresh_from_db()
        self.assertEqual(self.d.approved_by, self.other_lead)

    def test_only_approver_may_approve_own_version_and_it_is_audited(self):
        self.post_json(signed_in(self.lead), self.save_url, {"data": {"nodes": [node(1)]}, "base": 1})
        self.submit()
        signed_in(self.lead).post(self.status_url, {"action": "approve", "version": 2})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, "approved")
        log = AuditLog.objects.filter(action="diagram.approve").latest("pk")
        self.assertIn("only lead", log.details["self_approved"])
