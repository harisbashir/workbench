from django import forms

from apps.projects.models import Board

from .models import MechanicalPart


class PartForm(forms.ModelForm):
    class Meta:
        model = MechanicalPart
        fields = ["name", "kind", "part_number", "revision", "status", "process", "material", "finish",
                  "quantity", "supplier", "boards", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3}), "boards": forms.CheckboxSelectMultiple}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["boards"].queryset = Board.objects.filter(project=project)
        self.fields["boards"].label = "Boards it holds"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = MechanicalPart.objects.filter(project=self.project, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This product already has a part with that name.")
        return name
