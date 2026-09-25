from django import forms
from django.conf import settings

from apps.accounts.models import User

from .models import Project, Revision, Task, TaskComment


class DateInput(forms.DateInput):
    input_type = "date"


class ProjectForm(forms.ModelForm):
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.filter(is_active=True), required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="People who work on this project. Leads, admins and procurement can always see every project.",
    )

    class Meta:
        model = Project
        fields = ["name", "key", "description", "status", "lead", "members", "github_repo"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lead"].queryset = User.objects.filter(is_active=True)
        if self.instance.pk:
            self.fields["key"].disabled = True
            self.fields["key"].help_text = "The key can't change once tasks exist, so links keep working."

    def clean_key(self):
        return self.cleaned_data["key"].upper()


class RevisionForm(forms.ModelForm):
    class Meta:
        model = Revision
        fields = ["name", "status", "target_date", "git_ref", "notes"]
        widgets = {"target_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 3})}


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "kind", "description", "assignee", "reviewer", "priority", "status", "revision", "due_date"]
        widgets = {
            "due_date": DateInput(),
            "description": forms.Textarea(attrs={"rows": 6, "placeholder": "What needs doing, and how will we know it's done?"}),
            "title": forms.TextInput(attrs={"placeholder": "e.g. Route USB-C connector and add ESD protection"}),
        }
        labels = {"kind": "Type of work"}

    def __init__(self, project, *args, **kwargs):
        super().__init__(*args, **kwargs)
        people = User.objects.filter(is_active=True)
        # Only offer people who are on this project (falls back to everyone for a new, empty project).
        member_ids = set(project.members.values_list("pk", flat=True))
        if project.lead_id:
            member_ids.add(project.lead_id)
        if member_ids:
            people = people.filter(pk__in=member_ids)
        self.fields["assignee"].queryset = people
        self.fields["reviewer"].queryset = people
        self.fields["revision"].queryset = project.revisions.exclude(status=Revision.Status.OBSOLETE)
        self.fields["assignee"].empty_label = "Unassigned"
        self.fields["reviewer"].empty_label = "No reviewer"
        self.fields["revision"].empty_label = "Not tied to a revision"


class CommentForm(forms.ModelForm):
    class Meta:
        model = TaskComment
        fields = ["body"]
        labels = {"body": ""}
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a comment. Mention someone with @username."})}


class AttachmentForm(forms.Form):
    file = forms.FileField(help_text="Schematic PDFs, screenshots, test logs, datasheets. Max 25 MB.")

    ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".svg", ".txt", ".log", ".csv", ".zip", ".kicad_sch",
               ".kicad_pcb", ".kicad_pro", ".step", ".stp", ".gbr", ".drl", ".md", ".json", ".xlsx", ".docx"}

    def clean_file(self):
        f = self.cleaned_data["file"]
        import os
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in self.ALLOWED:
            raise forms.ValidationError(f"Files of type {ext or '(none)'} aren't allowed.")
        if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files must be under {settings.MAX_UPLOAD_MB} MB.")
        return f
