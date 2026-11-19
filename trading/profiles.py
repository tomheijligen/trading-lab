from decimal import Decimal
from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from PIL import Image, ImageOps

from .models import Account, Deposit, UserProfile, ZERO

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
AVATAR_SIZE = (160, 160)


def encode_avatar(upload):
    """Validate the image and return a small JPEG with no original metadata."""
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise forms.ValidationError("Choose an image of 2 MB or less.")

    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG")) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise forms.ValidationError("Choose an image with fewer than 12 million pixels.")
            portrait = ImageOps.exif_transpose(image).convert("RGB")
            portrait = ImageOps.fit(portrait, AVATAR_SIZE, Image.Resampling.LANCZOS)
            output = BytesIO()
            portrait.save(output, format="JPEG", quality=85)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise forms.ValidationError("Choose a valid JPG or PNG image.") from None


class ProfileForm(forms.Form):
    display_name = forms.CharField(label="Display name", max_length=80)
    photo = forms.FileField(
        label="Profile photo",
        help_text="JPG or PNG, up to 2 MB.",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png"}),
    )
    remove_photo = forms.BooleanField(label="Remove current photo", required=False)

    def clean(self):
        values = super().clean()
        photo = values.get("photo")
        if photo and values.get("remove_photo"):
            self.add_error("photo", "Choose either a new photo or removal.")
        elif photo:
            try:
                values["avatar_bytes"] = encode_avatar(photo)
            except forms.ValidationError as error:
                self.add_error("photo", error)
        return values


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def profile_page(request):
    saved = UserProfile.objects.filter(user=request.user).only("display_name").first()
    current_name = (
        (saved.display_name if saved else "")
        or request.user.get_full_name()
        or request.user.get_username().split("@", 1)[0]
    )

    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                profile, _ = UserProfile.objects.select_for_update().get_or_create(
                    user=request.user
                )
                profile.display_name = form.cleaned_data["display_name"]
                fields = ["display_name"]
                if form.cleaned_data.get("avatar_bytes"):
                    profile.avatar = form.cleaned_data["avatar_bytes"]
                    fields.append("avatar")
                elif form.cleaned_data["remove_photo"]:
                    profile.avatar = None
                    fields.append("avatar")
                profile.save(update_fields=fields)
            messages.success(request, "Profile saved.")
            return redirect("profile")
    else:
        form = ProfileForm(initial={"display_name": current_name})

    return render(request, "trading/page.html", {
        "title": "Your profile",
        "profile_form": form,
    })


@login_required
@never_cache
@require_http_methods(["GET"])
def profile_avatar(request):
    # This URL serves only the signed-in user's photo.
    avatar = UserProfile.objects.filter(user=request.user).values_list(
        "avatar", flat=True
    ).first()
    if not avatar:
        raise Http404
    return HttpResponse(bytes(avatar), content_type="image/jpeg")


def trading_navigation(request):
    """Build the header and sidebar using only the signed-in user's records."""
    if not request.user.is_authenticated:
        return {}

    profile = UserProfile.objects.filter(user=request.user).only("display_name").first()
    display_name = (
        (profile.display_name if profile else "")
        or request.user.get_full_name()
        or request.user.get_username().split("@", 1)[0]
    )
    has_avatar = (
        UserProfile.objects.filter(pk=profile.pk, avatar__isnull=False).exists()
        if profile else False
    )

    accounts = list(
        Account.objects.filter(owner=request.user)
        .order_by("mode", "name", "pk")
        .prefetch_related("instrument_set")
    )

    # Aggregate booked deposits separately to avoid counting them once per instrument.
    deposits = dict(
        Deposit.objects.filter(account__owner=request.user, booked=True)
        .values("account_id")
        .annotate(total=Sum("amount"))
        .values_list("account_id", "total")
    )

    sidebar = []
    for account in accounts:
        result = "—"
        tone = "neutral"
        if account.mode == "virtual":
            paid_in = account.starting_cash + deposits.get(account.pk, ZERO)
            if paid_in > ZERO:
                value = account.cash + account.reserve + sum(
                    (item.value for item in account.instrument_set.all()), ZERO
                )
                change = ((value - paid_in) / paid_in * 100).quantize(Decimal("0.01"))
                if change > ZERO:
                    result, tone = f"{change:+.2f}%", "gain"
                elif change < ZERO:
                    result, tone = f"{change:+.2f}%", "loss"
                else:
                    result = "0.00%"

        sidebar.append({
            "pk": account.pk,
            "name": account.name,
            "mode_label": "Virtual" if account.mode == "virtual" else "Real",
            "result": result,
            "tone": tone,
        })

    return {
        "lab_display_name": display_name,
        "lab_has_avatar": has_avatar,
        "lab_sidebar_accounts": sidebar,
    }