"""Controllers on boards and the firmware each board revision ships with."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.utils import audit
from apps.projects.models import Board, Controller, Revision, log_activity

from . import sets
from .models import ControllerSlot, Firmware
from .views import _project


def flash_order_for(fw):
    """Bootloaders are flashed before everything else."""
    return 1 if "boot" in fw.name.lower() else 2


class ControllerForm(forms.ModelForm):
    firmwares = forms.ModelMultipleChoiceField(
        queryset=Firmware.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Firmware it runs",
        help_text="Unticking a firmware also removes the version chosen for it on this board's revisions.")

    class Meta:
        model = Controller
        fields = ["designator", "name", "part", "notes", "order"]

    def __init__(self, *args, board, **kwargs):
        super().__init__(*args, **kwargs)
        self.board = board
        self.fields["firmwares"].queryset = board.project.firmwares.all()
        if self.instance.pk:
            self.fields["firmwares"].initial = [s.firmware_id for s in self.instance.slots.all()]
        if not board.project.firmwares.exists():
            self.fields["firmwares"].help_text = "This project has no firmware yet. Add it under Firmware, then come back."

    def clean_designator(self):
        d = self.cleaned_data["designator"].strip().upper()
        if Controller.objects.filter(board=self.board, designator=d).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"{d} is already on this board.")
        return d

    def save(self, commit=True):
        c = super().save(commit=False)
        c.board = self.board
        c.save()
        chosen = {fw.pk: fw for fw in self.cleaned_data["firmwares"]}
        c.slots.exclude(firmware_id__in=chosen).delete()
        have = set(c.slots.values_list("firmware_id", flat=True))
        for pk, fw in chosen.items():
            if pk not in have:
                ControllerSlot.objects.create(controller=c, firmware=fw, flash_order=flash_order_for(fw))
        return c


def _board(request, key, board, edit=False):
    project = _project(request, key, edit=edit)
    return project, get_object_or_404(Board, pk=board, project=project)


@login_required
def controller_edit(request, key, board, pk=None):
    project, b = _board(request, key, board, edit=True)
    c = get_object_or_404(Controller, pk=pk, board=b) if pk else None
    form = ControllerForm(request.POST or None, instance=c, board=b)
    if request.method == "POST" and form.is_valid():
        creating = c is None
        with transaction.atomic():
            c = form.save()
        audit(request, "controller.created" if creating else "controller.updated", c, board=b.name)
        if creating:
            log_activity(project, f"added controller {c} to {b.name}", actor=request.user, url=b.get_absolute_url())
            messages.success(request, f"{c} added. Choose its firmware version on each revision.")
        else:
            messages.success(request, f"{c} saved.")
        return redirect(b)
    return render(request, "firmware/controller_form.html", {"form": form, "project": project, "b": b, "c": c})


@login_required
@require_POST
def controller_delete(request, key, board, pk):
    project, b = _board(request, key, board, edit=True)
    c = get_object_or_404(Controller, pk=pk, board=b)
    audit(request, "controller.deleted", c, board=b.name)
    log_activity(project, f"removed controller {c} from {b.name}", actor=request.user, url=b.get_absolute_url())
    messages.success(request, f"{c} removed.")
    c.delete()
    return redirect(b)


# --- Firmware set of a revision ------------------------------------------------------

def _revision(request, key, pk, edit=False):
    project = _project(request, key, edit=edit)
    return project, get_object_or_404(Revision, pk=pk, project=project)


@login_required
@require_POST
def revision_set(request, key, pk):
    project, rev = _revision(request, key, pk, edit=True)
    choices = {}
    for name, value in request.POST.items():
        if name.startswith("slot_") and name[5:].isdigit():
            choices[int(name[5:])] = int(value) if value.isdigit() else None
    errors = sets.save_set(rev, choices, request.user)
    for e in errors:
        messages.error(request, e)
    if not errors:
        messages.success(request, f"Firmware for {rev.title} saved.")
    if errors != [sets.LOCKED_MESSAGE]:
        summary = ", ".join(f"{e.slot.controller.designator} {e.slot.firmware.name} {e.release.version}"
                            for e in rev.firmware_set.select_related("slot__controller", "slot__firmware", "release"))
        audit(request, "revision.firmware_set", rev, firmware=summary or "none")
        log_activity(project, f"set the firmware for {rev.title}", actor=request.user, url=rev.get_absolute_url())
    return redirect(rev)


@login_required
@require_POST
def revision_set_lock(request, key, pk):
    project, rev = _revision(request, key, pk, edit=True)
    if not request.user.can_manage_projects:
        raise PermissionDenied("Only a lead or an administrator can lock or unlock the firmware.")
    locking = request.POST.get("action") == "lock"
    if sets.set_lock(rev, locking):
        audit(request, "revision.firmware_set_lock" if locking else "revision.firmware_set_unlock", rev)
        messages.success(request, f"Firmware for {rev.title} {'locked' if locking else 'unlocked'}.")
    return redirect(rev)
