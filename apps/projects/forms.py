from django import forms

from apps.accounts.models import User

from .models import Board, Project, Revision, Task, TaskComment, default_board


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


class BoardForm(forms.ModelForm):
    class Meta:
        model = Board
        fields = ["name", "kind", "code", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"kind": "Type", "code": "Code / PCB part number"}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Board.objects.filter(project=self.project, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This product already has a board with that name.")
        return name


class RevisionForm(forms.ModelForm):
    class Meta:
        model = Revision
        fields = ["board", "name", "status", "target_date", "git_ref", "notes"]
        widgets = {"target_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["board"].queryset = Board.objects.filter(project=project)
        self.fields["board"].empty_label = None
        self.fields["board"].required = False  # defaults to the product's first board
        self.fields["board"].help_text = "Which PCB of the product this is a revision of."

    def clean(self):
        data = super().clean()
        if not data.get("board") and self.project is not None:
            data["board"] = self.instance.board if self.instance.board_id else default_board(self.project)
            self.instance.board = data["board"]
        board, name = data.get("board"), data.get("name")
        if board and name:
            qs = Revision.objects.filter(board=board, name__iexact=name.strip())
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error("name", f"{board.name} already has a revision called {name}.")
        return data


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "kind", "description", "assignee", "reviewer", "priority", "status", "revision", "due_date", "blocked_reason"]
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
