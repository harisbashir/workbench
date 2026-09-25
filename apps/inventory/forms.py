from django import forms

from .models import BomLine, Part, Supplier


class PartForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = ["ipn", "description", "category", "value", "footprint", "manufacturer", "mpn", "datasheet_url",
                  "supplier", "supplier_sku", "unit_cost", "min_stock", "location", "lifecycle", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"unit_cost": "Unit cost (USD)"}


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "kind", "website", "contact_name", "email", "phone", "country", "currency", "lead_time_days", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"kind": "Type"}


class StockAdjustForm(forms.Form):
    MODE = [("add", "Add to stock"), ("remove", "Remove from stock"), ("set", "Set exact count (after a stock count)")]
    mode = forms.ChoiceField(choices=MODE, widget=forms.RadioSelect, initial="add")
    quantity = forms.IntegerField(min_value=0)
    note = forms.CharField(max_length=120, required=False, help_text="Why? e.g. Found in drawer, damaged, annual count.")


class BomLineForm(forms.ModelForm):
    class Meta:
        model = BomLine
        fields = ["part", "quantity", "references", "dnp", "notes"]
        widgets = {"references": forms.TextInput(attrs={"placeholder": "R1, R2, R7"})}


class BomUploadForm(forms.Form):
    file = forms.FileField(label="BOM file (.csv)", help_text="Export from KiCad (Tools → Generate BOM → CSV) or use KiBot's CSV output.")
    replace = forms.BooleanField(required=False, initial=True,
                                 label="Replace the current BOM for this revision",
                                 help_text="Untick to add to the existing lines instead.")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".csv", ".tsv", ".txt")):
            raise forms.ValidationError("Please upload a .csv file.")
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("BOM files must be under 5 MB.")
        return f
