"""Validated input forms for the project and authentication surfaces."""

from django import forms

from ledger.models import AcceptanceItem, Project


class MagicLinkRequestForm(forms.Form):
    """Validate a freelancer email requesting a login link."""

    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class ProjectForm(forms.ModelForm):
    """Validate editable project details."""

    class Meta:
        """Configure the project fields exposed to freelancers."""

        model = Project
        fields = (
            "title",
            "client_name",
            "client_email",
            "brief",
            "skills_csv",
            "revision_limit",
        )
        widgets = {"brief": forms.Textarea(attrs={"rows": 6})}


class AcceptanceItemForm(forms.ModelForm):
    """Validate one ordered acceptance criterion."""

    class Meta:
        """Configure editable acceptance item fields."""

        model = AcceptanceItem
        fields = ("text", "order")
        widgets = {"text": forms.Textarea(attrs={"rows": 2})}


class ActionForm(forms.Form):
    """Validate an intentional POST action with no additional input."""
