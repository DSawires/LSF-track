"""What is kept of a picture: upright, without its metadata, as WebP -- from
any format a phone sends, HEIC included -- and the conversion that brings
pictures stored before this into line."""

from __future__ import annotations

import io
import uuid

import pytest

from app.models import ItemImage
from tests.pictures import close, decoded, picture


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    from app.storage import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()
    yield get_storage()
    get_settings.cache_clear()
    get_storage.cache_clear()


def _upload(client, item, data: bytes, name="p.jpg"):
    made = client.post(f"/api/items/{item.id}/images", files={"file": (name, data, "image/jpeg")})
    assert made.status_code == 201, made.text
    return made.json()


def test_an_iphone_heic_photo_is_stored_as_webp(world, client, storage):
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    out = io.BytesIO()
    Image.new("RGB", (80, 60), (40, 160, 40)).save(out, "HEIF")
    _factory, _stages, item = world

    body = _upload(client, item, out.getvalue(), "IMG_0001.HEIC")
    assert body["content_type"] == "image/webp"
    fmt, size, colour = decoded(client.get(body["url"]).content)
    assert (fmt, size) == ("WEBP", (80, 60)) and close(colour, (40, 160, 40), tolerance=20)


def test_a_photo_is_turned_upright_and_loses_its_metadata(world, client, storage):
    """Phones store "rotate this" and the GPS position in the metadata. The
    metadata does not survive, so the rotation is applied to the pixels."""
    from PIL import Image

    sideways = Image.new("RGB", (60, 30), (200, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = 6              # Orientation: rotate 90° clockwise to view
    exif[0x010F] = "PhoneMaker"   # Make
    exif[0x8825] = {2: (51.0, 30.0, 0.0)}  # GPSInfo: a latitude
    out = io.BytesIO()
    sideways.save(out, "JPEG", exif=exif)
    _factory, _stages, item = world

    body = _upload(client, item, out.getvalue())
    stored = client.get(body["url"]).content
    with Image.open(io.BytesIO(stored)) as kept:
        assert kept.size == (30, 60)  # upright
        assert not kept.getexif()     # nothing of the original's metadata
        assert "exif" not in kept.info


def test_a_small_picture_is_never_enlarged(world, client, storage):
    _factory, _stages, item = world
    body = _upload(client, item, picture("PNG", size=(100, 80)), "small.png")
    assert decoded(client.get(body["url"]).content)[1] == (100, 80)
    assert decoded(client.get(body["thumb_url"]).content)[1] == (100, 80)


def test_compress_images_converts_what_was_stored_before(world, client, storage):
    """`manage.py compress-images`: originals stored before compression become
    a display copy and a thumbnail; a picture already done is skipped, and one
    that cannot be decoded is left exactly as it was."""
    import manage
    from app.services.images import image_key, thumb_key

    factory, _stages, item = world
    old = picture("JPEG", size=(3000, 2000), color=(40, 40, 200))
    rows = {}
    for label, data in (("old", old), ("junk", b"\xff\xd8\xff" + b"not a jpeg" * 10)):
        image_id = uuid.uuid4()
        storage.put(image_key(image_id), io.BytesIO(data))
        factory.db.add(ItemImage(
            id=image_id, item_id=item.id, kind="icon", filename=f"{label}.jpg",
            content_type="image/jpeg", size_bytes=len(data), uploaded_by_user_id=factory.user.id,
        ))
        rows[label] = image_id
    done = _upload(client, item, picture())  # uploaded today: already converted
    factory.db.commit()
    before_done = storage.read(image_key(uuid.UUID(done["id"])))

    manage.cmd_compress_images(None)

    factory.db.expire_all()
    converted = factory.db.get(ItemImage, rows["old"])
    assert converted.content_type == "image/webp"
    display = storage.read(image_key(converted.id))
    assert converted.size_bytes == len(display) < len(old)
    assert decoded(display)[:2] == ("WEBP", (2048, 1365))
    assert decoded(storage.read(thumb_key(converted.id)))[1] == (320, 213)
    # The thumbnail is served for good now, not as a stand-in.
    assert "immutable" in client.get(f"/api/images/{converted.id}/thumb").headers["cache-control"]

    junk = factory.db.get(ItemImage, rows["junk"])
    assert junk.content_type == "image/jpeg"
    assert storage.read(thumb_key(junk.id)) is None
    assert storage.read(image_key(uuid.UUID(done["id"]))) == before_done  # skipped

    # Run again: nothing left to do, nothing re-encoded.
    again = storage.read(image_key(converted.id))
    manage.cmd_compress_images(None)
    assert storage.read(image_key(converted.id)) == again


def test_a_picture_not_yet_converted_stands_in_for_its_thumbnail(world, client, storage):
    """Between a deploy and `compress-images`, old pictures have no thumbnail.
    The whole picture is served in its place, and not cached as if final."""
    from app.services.images import image_key

    factory, _stages, item = world
    image_id = uuid.uuid4()
    original = picture("JPEG")
    storage.put(image_key(image_id), io.BytesIO(original))
    factory.db.add(ItemImage(
        id=image_id, item_id=item.id, kind="icon", filename="old.jpg",
        content_type="image/jpeg", size_bytes=len(original), uploaded_by_user_id=factory.user.id,
    ))
    factory.db.commit()

    served = client.get(f"/api/images/{image_id}/thumb")
    assert served.status_code == 200
    assert served.content == original
    assert served.headers["cache-control"] == "private, no-cache"
