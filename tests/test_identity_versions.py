"""Identity is immutable once locked, and rendered work stays pinned to what it used.

The failure this prevents: a persona's reference image is the single input that
decides who appears on screen. Overwriting it in place retroactively changes the
provenance of every clip delivered under the old face, with no way to tell afterwards
which version a given video actually used.
"""
import pytest

from app import db, identity, images, jobs


def _job(persona_id, brief="She smiles to camera"):
    return jobs.create_job(persona_id=persona_id, brief=brief)


def _asset(client_id, persona_id, url):
    return db.insert(
        """INSERT INTO assets (client_id, persona_id, kind, source, url)
           VALUES (?,?, 'image', 'generated', ?)""", (client_id, persona_id, url))


def test_a_persona_gets_a_locked_v1_on_first_use(persona_id):
    _job(persona_id)
    v = identity.current_version(persona_id)
    assert v is not None and v["version"] == 1 and v["status"] == "locked"


def test_a_job_pins_the_identity_it_was_briefed_against(persona_id):
    job_id = _job(persona_id)
    v1 = identity.current_version(persona_id)
    row = db.query_one("SELECT identity_version_id FROM jobs WHERE id=?", (job_id,))
    assert row["identity_version_id"] == v1["id"]


def test_editing_a_persona_creates_a_new_version_and_supersedes_the_old(persona_id):
    _job(persona_id)
    v1 = identity.current_version(persona_id)
    v2 = identity.edit(persona_id, "tester",
                       reference_image_url="https://example.test/new-face.png")

    assert v2["version"] == 2 and v2["status"] == "locked"
    assert identity.get_version(v1["id"])["status"] == "superseded"
    # The old version's snapshot is untouched — that is what immutable means.
    assert identity.get_version(v1["id"])["reference_image_url"] == \
        "https://example.test/rania-locked.png"


def test_an_old_job_still_renders_as_the_face_it_was_briefed_for(persona_id):
    """THE regression. A re-draft after an identity change must not swap the face."""
    job_id = _job(persona_id)
    original = identity.current_version(persona_id)["reference_image_url"]

    identity.edit(persona_id, "tester",
                  reference_image_url="https://example.test/totally-different.png")

    bundle = jobs._job_bundle(job_id)
    assert bundle["reference_image_url"] == original, (
        "an existing job re-drafted after an identity edit must render the person it "
        "was briefed for, not whoever the persona has since become")
    assert bundle["identity_version"] == 1


def test_a_new_job_uses_the_new_version(persona_id):
    _job(persona_id)
    identity.edit(persona_id, "tester",
                  reference_image_url="https://example.test/v2-face.png")
    new_job = _job(persona_id, "A second brief")
    bundle = jobs._job_bundle(new_job)
    assert bundle["reference_image_url"] == "https://example.test/v2-face.png"
    assert bundle["identity_version"] == 2


def test_promoting_an_asset_cuts_a_version_instead_of_overwriting(client_id, persona_id):
    _job(persona_id)
    aid = _asset(client_id, persona_id, "https://example.test/plate-a.png")
    images.set_primary(aid, "tester")

    versions = identity.versions(persona_id)
    assert len(versions) == 2, "promoting a face is an identity edit, not a mutation"
    assert identity.current_version(persona_id)["reference_image_url"] == \
        "https://example.test/plate-a.png"


def test_generations_record_which_identity_they_used(persona_id):
    job_id = _job(persona_id)
    jobs.start_draft(job_id)
    jobs.wait_idle(20)
    v1 = db.query_one("SELECT identity_version_id FROM jobs WHERE id=?", (job_id,))
    gen = db.query_one("SELECT identity_version_id FROM generations WHERE job_id=?",
                       (job_id,))
    assert gen["identity_version_id"] == v1["identity_version_id"]


def test_an_identity_that_could_not_generate_cannot_be_locked(client_id):
    pid = db.insert(
        """INSERT INTO personas (client_id, name, identity_strategy)
           VALUES (?,?, 'reference_image')""", (client_id, "Faceless"))
    identity.open_draft(pid, reference_image_url=None)
    with pytest.raises(identity.IdentityLocked):
        identity.lock(pid, "tester")


def test_a_plate_backing_a_locked_version_cannot_be_deleted(client_id, persona_id):
    _job(persona_id)
    aid = _asset(client_id, persona_id, "https://example.test/plate-b.png")
    images.set_primary(aid, "tester")            # becomes the v2 identity plate
    other = _asset(client_id, persona_id, "https://example.test/plate-c.png")
    images.set_primary(other, "tester")          # v3; demotes the first

    with pytest.raises(images.ImageError, match="identity plate of locked"):
        images.delete_asset(aid)


def test_an_ordinary_still_is_still_deletable(client_id, persona_id):
    """Recording which identity was current is provenance, not a preservation order."""
    _job(persona_id)
    aid = _asset(client_id, persona_id, "https://example.test/just-a-photo.png")
    db.execute("UPDATE assets SET identity_version_id=? WHERE id=?",
               (identity.current_version(persona_id)["id"], aid))
    images.delete_asset(aid)                      # must not raise
    assert db.query_one("SELECT 1 FROM assets WHERE id=?", (aid,)) is None


def test_versions_are_numbered_without_gaps(persona_id):
    _job(persona_id)
    for i in range(3):
        identity.edit(persona_id, "tester",
                      reference_image_url=f"https://example.test/v{i}.png")
    assert [v["version"] for v in identity.versions(persona_id)] == [4, 3, 2, 1]
