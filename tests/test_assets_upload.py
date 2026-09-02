"""Uploads, wardrobe, and the boundary between the locked and variable layers.

The platform spec is blunt about this: "The clean separation between locked and
variable is what makes the product work. Never let a variable leak into the identity
layer." A pair of glasses is not part of who someone is — and treating it as if it
were means every future render inherits it and it cannot be deleted.
"""
import pytest

from app import db, identity, images
from app.providers.mock import _placeholder_png


def _png() -> bytes:
    return _placeholder_png("test-plate")


def _upload(persona_id=None, client_id=None, plate=None, label=""):
    return images.upload_file(
        data=_png(), content_type="image/png", filename="plate.png",
        persona_id=persona_id, client_id=client_id, plate=plate, label=label)


def test_an_uploaded_image_becomes_an_asset(client_id, persona_id):
    a = _upload(persona_id=persona_id, label="a still")
    assert a["source"] == "uploaded" and a["kind"] == "image" and a["url"]
    assert a["persona_id"] == persona_id and a["client_id"] == client_id


def test_an_identity_plate_lands_in_a_draft_not_a_locked_version(client_id, persona_id):
    """Uploading a picture must never silently rewrite a locked identity."""
    identity.edit(persona_id, "tester", reference_image_url="https://x.test/v1.png")
    locked = identity.current_version(persona_id)

    a = _upload(persona_id=persona_id, plate="identity", label="new plate")

    assert a["identity_version_id"] != locked["id"], "it must not attach to the lock"
    draft = identity.draft_version(persona_id)
    assert draft is not None and a["identity_version_id"] == draft["id"]
    assert identity.current_version(persona_id)["id"] == locked["id"], (
        "the locked version must be untouched until someone locks the draft")


def test_wardrobe_is_not_attached_to_any_identity_version(client_id, persona_id):
    """The variable layer stays out of the locked layer — spec section 1."""
    a = _upload(persona_id=persona_id, plate="wardrobe", label="tortoiseshell glasses")
    assert a["identity_version_id"] is None


def test_wardrobe_stays_deletable_even_after_the_identity_is_locked(client_id, persona_id):
    identity.edit(persona_id, "tester", reference_image_url="https://x.test/v1.png")
    a = _upload(persona_id=persona_id, plate="wardrobe", label="gold wristwatch")
    images.delete_asset(a["id"])                       # must not raise
    assert db.query_one("SELECT 1 FROM assets WHERE id=?", (a["id"],)) is None


def test_an_identity_plate_backing_a_locked_version_is_protected(client_id, persona_id):
    _upload(persona_id=persona_id, plate="identity")
    identity.lock(persona_id, "tester")
    plate = images.list_assets(persona_id=persona_id, plates=("identity",))[0]
    with pytest.raises(images.ImageError, match="identity plate of locked"):
        images.delete_asset(plate["id"])


def test_an_identity_plate_needs_an_influencer_to_belong_to(client_id):
    with pytest.raises(images.ImageError, match="belong to one"):
        _upload(client_id=client_id, plate="identity")


def test_a_product_plate_needs_no_persona(client_id):
    a = _upload(client_id=client_id, plate="product", label="serum bottle")
    assert a["persona_id"] is None and a["plate"] == "product"


def test_an_unknown_plate_is_refused(client_id, persona_id):
    with pytest.raises(images.ImageError, match="plate must be one of"):
        _upload(persona_id=persona_id, plate="hairstyle")


def test_a_non_image_upload_is_refused(client_id, persona_id):
    with pytest.raises(images.ImageError, match="unsupported file type"):
        images.upload_file(data=b"not an image", content_type="application/pdf",
                           persona_id=persona_id)


def test_an_empty_upload_is_refused(client_id, persona_id):
    with pytest.raises(images.ImageError, match="empty"):
        images.upload_file(data=b"", content_type="image/png", persona_id=persona_id)


def test_an_oversized_upload_is_refused(client_id, persona_id):
    from app.storage import MAX_UPLOAD_BYTES
    with pytest.raises(images.ImageError, match="limit is"):
        images.upload_file(data=b"\x00" * (MAX_UPLOAD_BYTES + 1),
                           content_type="image/png", persona_id=persona_id)


def test_the_two_layers_are_listed_separately(client_id, persona_id):
    _upload(persona_id=persona_id, plate="identity")
    _upload(persona_id=persona_id, plate="wardrobe", label="watch")
    _upload(persona_id=persona_id)                      # a plain still

    variable = images.list_assets(persona_id=persona_id,
                                  plates=identity.VARIABLE_PLATES)
    locked_and_stills = images.list_assets(persona_id=persona_id,
                                           exclude_plates=identity.VARIABLE_PLATES)
    assert [a["plate"] for a in variable] == ["wardrobe"]
    assert sorted(a["plate"] or "" for a in locked_and_stills) == ["", "identity"]


def test_the_character_sheet_edits_a_draft_and_leaves_the_lock_alone(client_id, persona_id):
    import json
    identity.edit(persona_id, "tester", reference_image_url="https://x.test/v1.png")
    locked = identity.current_version(persona_id)

    identity.open_draft(persona_id, character_sheet=json.dumps({"hair": "curly, dark"}))

    assert identity.current_version(persona_id)["id"] == locked["id"]
    assert identity.sheet(identity.draft_version(persona_id))["hair"] == "curly, dark"
    assert identity.sheet(locked) == {}, "the locked version's sheet must not change"
