from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache

from apps.core.storage import file_response

from .models import MeshStatus

# kind -> (model label, how to find the owning project)
KINDS = {
    "design": ("design.DesignFile", lambda o: o.revision.project),
    "mech": ("mechanical.MechanicalFile", lambda o: o.part.project),
}


def _object(request, kind, pk):
    if kind not in KINDS:
        raise Http404
    label, project_of = KINDS[kind]
    try:
        model = apps.get_model(label)
    except LookupError:
        raise Http404
    obj = get_object_or_404(model, pk=pk)
    if not project_of(obj).can_view(request.user):
        raise PermissionDenied
    return obj


@login_required
def mesh(request, kind, pk):
    """The converted model, gzip-compressed; the browser unpacks it."""
    obj = _object(request, kind, pk)
    if obj.mesh_status != MeshStatus.READY or not obj.mesh:
        raise Http404("This model isn't ready to view.")
    etag = f'"{obj.sha256}"'
    if request.headers.get("If-None-Match") == etag:
        from django.http import HttpResponseNotModified
        return HttpResponseNotModified()
    resp = file_response(obj.mesh, filename="model.wbm", inline=True, content_type="application/octet-stream")
    resp["Content-Encoding"] = "gzip"
    resp["Cache-Control"] = "private, max-age=86400"
    resp["ETag"] = etag
    return resp


@login_required
def thumb(request, kind, pk):
    obj = _object(request, kind, pk)
    if not obj.thumb:
        raise Http404
    resp = file_response(obj.thumb, filename="preview.png", inline=True, content_type="image/png")
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


@never_cache
@login_required
def status(request, kind, pk):
    obj = _object(request, kind, pk)
    return JsonResponse({"status": obj.mesh_status, "message": obj.mesh_message, "info": obj.mesh_info})
