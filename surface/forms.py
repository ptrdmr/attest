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


class DeliveryItemForm(forms.ModelForm):
    """Validate delivery status and an optional evidence link."""

    is_passed = forms.TypedChoiceField(
        label="Delivery result",
        choices=((True, "Passed"), (False, "Not passed")),
        coerce=lambda value: value == "True",
    )

    class Meta:
        """Expose only delivery fields on an active project."""

        model = AcceptanceItem
        fields = ("is_passed", "evidence_url")
        widgets = {"evidence_url": forms.URLInput(attrs={"placeholder": "https://…"})}


class ClientApprovalForm(forms.Form):
    """Validate the rendered submitted-item batch fingerprint."""

    batch_fingerprint = forms.CharField(widget=forms.HiddenInput)


class ChangeOrderForm(forms.Form):
    """Validate a proposed project price and timeline adjustment."""

    description = forms.CharField(
        label="Change description",
        strip=True,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    amount_cents = forms.IntegerField(label="Additional amount (cents)", min_value=0)
    timeline_days = forms.IntegerField(label="Additional timeline (days)", min_value=0)


class ChangeOrderDecisionForm(forms.Form):
    """Validate a client's explicit change-order decision."""

    decision = forms.ChoiceField(
        choices=(("approve", "Approve"), ("decline", "Decline")),
    )


class ProfileVisibilityForm(forms.Form):
    """Validate an owner's explicit profile visibility intent."""

    intent = forms.ChoiceField(
        choices=(("publish", "Publish"), ("unpublish", "Unpublish")),
        widget=forms.HiddenInput,
    )


class SignatureForm(forms.Form):
    """Validate the client's typed electronic signature."""

    signature_name = forms.CharField(
        label="Type your full name",
        max_length=255,
        strip=True,
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    confirm = forms.BooleanField(
        label="I confirm this delivery record is accurate and sign it electronically."
    )


class AiDumpForm(forms.Form):
    """Validate pasted email or Slack source text for AI drafting."""

    source_dump = forms.CharField(
        label="Paste email or Slack thread",
        strip=False,
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "placeholder": "Paste the conversation, bullets, or deliverables…",
            }
        ),
    )


class AiDraftConfirmForm(forms.Form):
    """Validate freelancer-edited AI draft output before it is applied."""

    brief = forms.CharField(
        label="Project brief",
        strip=True,
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    criteria_text = forms.CharField(
        label="Acceptance criteria (one per line)",
        strip=True,
        widget=forms.Textarea(attrs={"rows": 8}),
    )

    def criteria_lines(self):
        """Return non-empty criterion lines in display order."""
        return [
            line.strip()
            for line in self.cleaned_data["criteria_text"].splitlines()
            if line.strip()
        ]


class ActionForm(forms.Form):
    """Validate an intentional POST action with no additional input."""
