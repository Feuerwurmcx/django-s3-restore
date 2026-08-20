"""Tests fuer die Admin-Seite zur S3-Wiederherstellung (moto, kein echtes S3)."""
import re
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import boto3
import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone as djtz
from moto import mock_aws

BUCKET = "garten-backup"
NAME = "config/zones.json"

CHANGELIST = "/admin/s3restore/s3version/"
VERSIONS = "/admin/s3restore/s3version/versions/"
RESTORE = "/admin/s3restore/s3version/restore/"
BULK = "/admin/s3restore/s3version/bulk/"


@pytest.fixture
def storage(db):
    with mock_aws():
        client = boto3.client("s3", region_name="eu-central-1")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        from django.core.files.storage import storages
        storages._storages.clear()
        yield storages["default"]
        storages._storages.clear()


@pytest.fixture
def admin_client(client, db):
    user = User.objects.create_superuser("chef", "chef@example.com", "pw")
    client.force_login(user)
    return client


def put(storage, text, name=NAME):
    time.sleep(1.05)  # S3-Zeitstempel loesen nur auf Sekunden auf
    storage.save(name, ContentFile(text.encode()))


def read(storage, name=NAME):
    with storage.open(name) as fh:
        return fh.read().decode()


def flat(html: str) -> str:
    """HTML mit normalisierten Leerzeichen -- Templates umbrechen Saetze."""
    return re.sub(r"\s+", " ", html)


def versions_url(name=NAME, alias="default"):
    return VERSIONS + "?" + urlencode({"storage": alias, "name": name})


# ------------------------------------------------------------------- Uebersicht

def test_url_names_resolve():
    assert reverse("admin:s3restore_s3version_changelist") == CHANGELIST
    assert reverse("admin:s3restore_s3version_versions") == VERSIONS
    assert reverse("admin:s3restore_s3version_restore") == RESTORE


def test_changelist_lists_objects(storage, admin_client):
    put(storage, "v1"); put(storage, "v2")
    put(storage, "log1", "logs/x.txt")
    resp = admin_client.get(CHANGELIST, {"prefix": "config/"})
    body = resp.content.decode()
    assert resp.status_code == 200
    assert NAME in body and "logs/x.txt" not in body
    assert BUCKET in body


def test_changelist_marks_deleted_objects(storage, admin_client):
    put(storage, "v1")
    storage.delete(NAME)
    body = admin_client.get(CHANGELIST).content.decode()
    assert "geloescht" in body


def test_appears_on_admin_index(storage, admin_client):
    body = admin_client.get("/admin/").content.decode()
    assert "S3-Wiederherstellung" in body


def test_unknown_storage_alias_is_rejected(storage, admin_client):
    assert admin_client.get(CHANGELIST, {"storage": "boese"}).status_code == 400


# --------------------------------------------------------------- Versionsseite

def test_versions_page_shows_history(storage, admin_client):
    put(storage, "v1"); put(storage, "v2")
    body = admin_client.get(versions_url()).content.decode()
    assert "aktuell" in body
    assert body.count('name="version_id"') == 1        # nur die aeltere waehlbar
    assert "X-Amz-Signature" in body                   # presigned Download-Links


def test_versions_page_404_for_unknown_object(storage, admin_client):
    put(storage, "v1")
    assert admin_client.get(versions_url("gibts/nicht.json")).status_code == 404


# ------------------------------------------------------------------- Rollback

def test_restore_two_step_flow(storage, admin_client):
    put(storage, "v1"); put(storage, "v2")
    old = storage.versions(NAME)[-1]

    confirm = admin_client.post(RESTORE, {
        "storage": "default", "name": NAME, "version_id": old.version_id})
    assert confirm.status_code == 200
    assert "Ja, wiederherstellen" in confirm.content.decode()
    assert read(storage) == "v2"                       # Schritt 1 aendert nichts

    done = admin_client.post(RESTORE, {
        "storage": "default", "name": NAME,
        "version_id": old.version_id, "confirm": "yes"}, follow=True)
    assert done.status_code == 200
    assert read(storage) == "v1"
    assert "zurueckgesetzt" in done.content.decode()


def test_restore_writes_log_entry(storage, admin_client):
    put(storage, "v1"); put(storage, "v2")
    old = storage.versions(NAME)[-1]
    admin_client.post(RESTORE, {"storage": "default", "name": NAME,
                                "version_id": old.version_id, "confirm": "yes"})
    entry = LogEntry.objects.get()
    assert entry.object_id == NAME
    assert old.version_id in entry.change_message


def test_restore_keeps_full_history(storage, admin_client):
    put(storage, "v1"); put(storage, "v2")
    old = storage.versions(NAME)[-1]
    admin_client.post(RESTORE, {"storage": "default", "name": NAME,
                                "version_id": old.version_id, "confirm": "yes"})
    assert len(storage.versions(NAME)) == 3            # nichts geloescht


def test_restore_undeletes_object(storage, admin_client):
    put(storage, "v1")
    storage.delete(NAME)
    old = [v for v in storage.versions(NAME) if not v.is_delete_marker][0]
    admin_client.post(RESTORE, {"storage": "default", "name": NAME,
                                "version_id": old.version_id, "confirm": "yes"})
    assert storage.exists(NAME) and read(storage) == "v1"


def test_restore_of_current_version_reports_nothing_changed(storage, admin_client):
    put(storage, "v1")
    current = storage.versions(NAME)[0]
    resp = admin_client.post(RESTORE, {"storage": "default", "name": NAME,
                                       "version_id": current.version_id,
                                       "confirm": "yes"}, follow=True)
    assert "Nichts geaendert" in resp.content.decode()


def test_restore_of_unknown_version_is_404(storage, admin_client):
    put(storage, "v1")
    resp = admin_client.post(RESTORE, {"storage": "default", "name": NAME,
                                       "version_id": "gibtsnicht", "confirm": "yes"})
    assert resp.status_code == 404


# --------------------------------------------------------------------- Rechte

def test_get_on_restore_is_refused(storage, admin_client):
    assert admin_client.get(RESTORE).status_code == 400


def test_anonymous_is_redirected_to_login(storage, client):
    resp = client.get(CHANGELIST)
    assert resp.status_code == 302 and "/admin/login/" in resp["Location"]


def test_staff_without_permission_has_no_access(storage, client, db):
    user = User.objects.create_user("gast", password="pw", is_staff=True)
    client.force_login(user)
    assert client.get(CHANGELIST).status_code == 403
    assert "S3-Wiederherstellung" not in client.get("/admin/").content.decode()


def test_view_permission_allows_looking_but_not_restoring(storage, client, db):
    put(storage, "v1"); put(storage, "v2")
    user = User.objects.create_user("leser", password="pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_s3version"))
    client.force_login(user)

    body = client.get(versions_url()).content.decode()
    assert "aktuell" in body
    assert 'name="version_id"' not in body             # keine Auswahl angeboten
    assert "restore_s3version" in body                 # Hinweis auf fehlendes Recht

    old = storage.versions(NAME)[-1]
    assert client.post(RESTORE, {"storage": "default", "name": NAME,
                                 "version_id": old.version_id,
                                 "confirm": "yes"}).status_code == 403
    assert read(storage) == "v2"


def test_restore_permission_is_enough(storage, client, db):
    put(storage, "v1"); put(storage, "v2")
    user = User.objects.create_user("operator", password="pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_s3version"),
                              Permission.objects.get(codename="restore_s3version"))
    client.force_login(user)
    old = storage.versions(NAME)[-1]
    client.post(RESTORE, {"storage": "default", "name": NAME,
                          "version_id": old.version_id, "confirm": "yes"})
    assert read(storage) == "v1"


def test_csrf_token_is_required(storage, admin_client, settings):
    put(storage, "v1"); put(storage, "v2")
    old = storage.versions(NAME)[-1]
    from django.test import Client
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(User.objects.get(username="chef"))
    resp = strict.post(RESTORE, {"storage": "default", "name": NAME,
                                 "version_id": old.version_id, "confirm": "yes"})
    assert resp.status_code == 403
    assert read(storage) == "v2"


# --------------------------------------------------------------- Sammelaktion

def seed_three(storage):
    """a und b haben zwei Versionen, c nur eine."""
    put(storage, "a-alt", "config/a.json"); put(storage, "a-neu", "config/a.json")
    put(storage, "b-alt", "config/b.json"); put(storage, "b-neu", "config/b.json")
    put(storage, "c-einzig", "config/c.json")


def test_bulk_url_resolves():
    assert reverse("admin:s3restore_s3version_bulk") == BULK


def test_changelist_has_checkboxes_and_action(storage, admin_client):
    seed_three(storage)
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert body.count('name="names"') == 2          # c.json hat nur eine Version
    assert 'id="action-toggle"' in body
    assert "Vorherige Version wiederherstellen" in body
    assert 'value="restore_previous"' in body


def test_bulk_confirmation_lists_plan_and_skips(storage, admin_client):
    seed_three(storage)
    resp = admin_client.post(BULK, {
        "storage": "default", "prefix": "config/", "action": "restore_previous",
        "names": ["config/a.json", "config/b.json", "config/c.json"]})
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Ja, 2 Datei(en) zuruecksetzen" in body
    assert "Wird uebersprungen" in body and "config/c.json" in body
    # noch nichts passiert
    assert read(storage, "config/a.json") == "a-neu"


def test_bulk_restores_selected_files(storage, admin_client):
    seed_three(storage)
    resp = admin_client.post(BULK, {
        "storage": "default", "prefix": "config/", "action": "restore_previous",
        "confirm": "yes",
        "names": ["config/a.json", "config/b.json", "config/c.json"]}, follow=True)
    assert read(storage, "config/a.json") == "a-alt"
    assert read(storage, "config/b.json") == "b-alt"
    assert read(storage, "config/c.json") == "c-einzig"      # unveraendert
    body = resp.content.decode()
    assert "2 Datei(en) auf die vorherige Version zurueckgesetzt" in body
    assert "es gibt nur eine Version" in body


def test_bulk_only_touches_selected_files(storage, admin_client):
    seed_three(storage)
    admin_client.post(BULK, {"storage": "default", "prefix": "config/",
                             "action": "restore_previous", "confirm": "yes",
                             "names": ["config/a.json"]})
    assert read(storage, "config/a.json") == "a-alt"
    assert read(storage, "config/b.json") == "b-neu"


def test_bulk_writes_one_log_entry_per_file(storage, admin_client):
    seed_three(storage)
    admin_client.post(BULK, {"storage": "default", "prefix": "config/",
                             "action": "restore_previous", "confirm": "yes",
                             "names": ["config/a.json", "config/b.json"]})
    assert sorted(LogEntry.objects.values_list("object_id", flat=True)) == [
        "config/a.json", "config/b.json"]


def test_bulk_undeletes_selected_file(storage, admin_client):
    put(storage, "v1", "config/a.json")
    storage.delete("config/a.json")
    admin_client.post(BULK, {"storage": "default", "prefix": "config/",
                             "action": "restore_previous", "confirm": "yes",
                             "names": ["config/a.json"]})
    assert storage.exists("config/a.json") and read(storage, "config/a.json") == "v1"


def test_bulk_without_selection_warns(storage, admin_client):
    seed_three(storage)
    resp = admin_client.post(BULK, {"storage": "default", "prefix": "config/",
                                    "action": "restore_previous"}, follow=True)
    assert "Keine Datei ausgewaehlt" in resp.content.decode()


def test_bulk_rejects_get(storage, admin_client):
    assert admin_client.get(BULK).status_code == 400


def test_bulk_rejects_unknown_action(storage, admin_client):
    seed_three(storage)
    assert admin_client.post(BULK, {"storage": "default", "action": "loeschen",
                                    "names": ["config/a.json"]}).status_code == 400


def test_bulk_rejects_too_many_names(storage, admin_client):
    resp = admin_client.post(BULK, {"storage": "default", "action": "restore_previous",
                                    "names": [f"f{i}.txt" for i in range(201)]})
    assert resp.status_code == 400


def test_bulk_needs_restore_permission(storage, client, db):
    seed_three(storage)
    user = User.objects.create_user("leser2", password="pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_s3version"))
    client.force_login(user)

    body = client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert 'name="names"' not in body                     # keine Checkboxen
    assert "Vorherige Version wiederherstellen" not in body

    resp = client.post(BULK, {"storage": "default", "action": "restore_previous",
                              "confirm": "yes", "names": ["config/a.json"]})
    assert resp.status_code == 403
    assert read(storage, "config/a.json") == "a-neu"


# ------------------------------------------------- Blaettern per S3-KeyMarker

@pytest.fixture
def many_files(storage):
    """120 Objekte mit je zwei Versionen -- schnell per put_object, ohne sleep."""
    for i in range(120):
        for body in (b"alt", b"x"):
            storage.s3_client.put_object(
                Bucket=BUCKET, Key=storage.key_for(f"config/datei-{i:03d}.txt"),
                Body=body)
    return storage


def marker_of(url: str) -> str:
    return parse_qs(urlsplit(url).query).get("marker", [""])[0]


def next_url(body: str) -> str:
    """Ziel des 'Weiter'-Links aus dem gerenderten HTML."""
    match = re.search(r'href="([^"]+)">Weiter', body)
    return unescape(match.group(1)) if match else ""


def prev_url(body: str) -> str:
    match = re.search(r'href="([^"]+)">&lsaquo; Zurueck', body)
    return unescape(match.group(1)) if match else ""


def test_first_page_shows_page_size_rows(many_files, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert body.count('name="names"') == 50
    assert "config/datei-000.txt" in body and "config/datei-050.txt" not in body
    assert "Seite 1" in body and "50 Objekte auf dieser Seite" in body
    assert next_url(body) and not prev_url(body)


def test_next_marker_is_last_key_of_page(many_files, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert marker_of(next_url(body)) == "media/config/datei-049.txt"


def test_walking_forward_covers_all_objects(many_files, admin_client):
    seen, url, pages = [], CHANGELIST + "?prefix=config/", 0
    while url:
        body = admin_client.get(url).content.decode()
        seen += re.findall(r'name="names" value="([^"]+)"', body)
        url, pages = next_url(body), pages + 1
    assert pages == 3
    assert len(seen) == len(set(seen)) == 120
    assert seen[0] == "config/datei-000.txt" and seen[-1] == "config/datei-119.txt"


def test_last_page_has_no_next_link(many_files, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    body = admin_client.get(next_url(body)).content.decode()
    last = admin_client.get(next_url(body)).content.decode()
    assert last.count('name="names"') == 20
    assert not next_url(last) and "Ende der Liste" in last
    assert "Seite 3" in last


def test_back_link_returns_to_previous_page(many_files, admin_client):
    first = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    second = admin_client.get(next_url(first)).content.decode()
    assert "Seite 2" in second
    back = admin_client.get(prev_url(second)).content.decode()
    assert "Seite 1" in back
    assert "config/datei-000.txt" in back


def test_page_counter_resets_on_first_page(many_files, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    body = admin_client.get(next_url(body)).content.decode()
    assert "Seite 2" in body
    again = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert "Seite 1" in again and not prev_url(again)


def test_unknown_marker_still_renders(many_files, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "marker": "media/config/datei-100.txt"}).content.decode()
    assert body.count('name="names"') == 19          # 101..119
    assert "config/datei-101.txt" in body and not next_url(body)


def test_no_paginator_links_for_single_page(storage, admin_client):
    put(storage, "v1", "config/a.json"); put(storage, "v2", "config/a.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert "1 Objekt auf dieser Seite" in body
    assert not next_url(body) and not prev_url(body)
    assert "Auswahl gilt nur fuer diese Seite" not in body


def test_bulk_returns_to_same_marker(many_files, admin_client):
    storage = many_files
    time.sleep(1.05)
    storage.s3_client.put_object(Bucket=BUCKET,
                                 Key=storage.key_for("config/datei-050.txt"), Body=b"neu")
    marker = "media/config/datei-049.txt"
    resp = admin_client.post(BULK, {
        "storage": "default", "prefix": "config/", "marker": marker,
        "action": "restore_previous", "confirm": "yes",
        "names": ["config/datei-050.txt"]})
    assert resp.status_code == 302
    assert marker_of(resp["Location"]) == marker
    # die beiden Vorgaengerversionen liegen in derselben Sekunde -- Hauptsache,
    # es ging einen Schritt zurueck
    assert read(storage, "config/datei-050.txt") in ("x", "alt")


def test_bulk_confirmation_keeps_marker(many_files, admin_client):
    body = admin_client.post(BULK, {
        "storage": "default", "prefix": "config/", "marker": "media/config/datei-049.txt",
        "action": "restore_previous",
        "names": ["config/datei-050.txt"]}).content.decode()
    assert 'name="marker" value="media/config/datei-049.txt"' in body


# --------------------------------------------- page_under() direkt (Storage)

def test_page_under_returns_batch_and_marker(many_files):
    grouped, marker = many_files.page_under("config/", limit=10)
    assert list(grouped)[:2] == ["config/datei-000.txt", "config/datei-001.txt"]
    assert len(grouped) == 10
    assert marker == "media/config/datei-009.txt"


def test_page_under_last_batch_has_no_marker(many_files):
    grouped, marker = many_files.page_under(
        "config/", key_marker="media/config/datei-109.txt", limit=50)
    assert len(grouped) == 10 and marker is None


def test_page_under_keeps_histories_complete_across_batches(storage):
    """Ein Key mit vielen Versionen darf nicht an der Batch-Grenze zerreissen."""
    for name in ("config/a.txt", "config/b.txt", "config/c.txt"):
        for i in range(5):
            storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for(name),
                                         Body=f"{name}-{i}".encode())
    grouped, marker = storage.page_under("config/", limit=2, batch_size=2)
    assert list(grouped) == ["config/a.txt", "config/b.txt"]
    assert [len(h) for h in grouped.values()] == [5, 5]      # vollstaendig
    assert marker == "media/config/b.txt"

    rest, marker2 = storage.page_under("config/", key_marker=marker, limit=2, batch_size=2)
    assert list(rest) == ["config/c.txt"] and len(rest["config/c.txt"]) == 5
    assert marker2 is None


def test_page_under_ignores_other_prefixes(storage):
    storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for("config/a.txt"), Body=b"x")
    storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for("logs/b.txt"), Body=b"x")
    grouped, _ = storage.page_under("config/", limit=50)
    assert list(grouped) == ["config/a.txt"]


def test_page_under_skips_marker_key_itself(storage):
    """Der Marker-Key darf nicht doppelt erscheinen -- egal wie das Backend
    KeyMarker auslegt (AWS beginnt dahinter, moto liefert ihn mit)."""
    for name in ("config/a.txt", "config/b.txt"):
        storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for(name), Body=b"x")
    grouped, _ = storage.page_under("config/", key_marker="media/config/a.txt", limit=50)
    assert list(grouped) == ["config/b.txt"]


# ------------------------------------------------- Ganzen Pfad wiederherstellen

PATH_URL = "/admin/s3restore/s3version/path/"


@pytest.fixture
def photo_path(storage):
    """netzwerk/photos/ mit Unterordner, zwei Versionen je Datei."""
    for name in ("netzwerk/photos/a.jpg", "netzwerk/photos/2024/b.jpg",
                 "netzwerk/docs/c.pdf"):
        put(storage, "alt", name)
    for name in ("netzwerk/photos/a.jpg", "netzwerk/photos/2024/b.jpg",
                 "netzwerk/docs/c.pdf"):
        put(storage, "neu", name)
    return storage


def test_path_url_resolves():
    assert reverse("admin:s3restore_s3version_path") == PATH_URL


def test_changelist_offers_path_form_with_prefix(photo_path, admin_client):
    body = admin_client.get(CHANGELIST, {"prefix": "netzwerk/photos/"}).content.decode()
    assert "Ganzen Pfad wiederherstellen" in body
    assert 'name="mode" value="steps"' in body and 'name="mode" value="at"' in body
    assert 'type="datetime-local"' in body


def test_changelist_without_prefix_explains_instead(photo_path, admin_client):
    body = admin_client.get(CHANGELIST).content.decode()
    assert "Gib oben einen Praefix an" in body
    assert 'name="mode" value="steps"' not in body


def test_path_confirmation_counts_recursively(photo_path, admin_client):
    body = admin_client.post(PATH_URL, {"storage": "default",
                                        "prefix": "netzwerk/photos/",
                                        "mode": "steps"}).content.decode()
    assert "Ja, 2 Datei(en) zuruecksetzen" in body
    assert "netzwerk/photos/a.jpg" in body and "netzwerk/photos/2024/b.jpg" in body
    assert "netzwerk/docs/c.pdf" not in body
    assert read(photo_path, "netzwerk/photos/a.jpg") == "neu"   # noch nichts passiert


def test_path_restores_whole_subtree(photo_path, admin_client):
    resp = admin_client.post(PATH_URL, {"storage": "default",
                                        "prefix": "netzwerk/photos/",
                                        "mode": "steps", "confirm": "yes"}, follow=True)
    assert read(photo_path, "netzwerk/photos/a.jpg") == "alt"
    assert read(photo_path, "netzwerk/photos/2024/b.jpg") == "alt"
    assert read(photo_path, "netzwerk/docs/c.pdf") == "neu"     # anderer Pfad
    assert "2 Datei(en) auf die jeweils vorherige Version zurueckgesetzt" in resp.content.decode()


def test_path_brings_deleted_files_back(photo_path, admin_client):
    photo_path.delete("netzwerk/photos/a.jpg")
    assert not photo_path.exists("netzwerk/photos/a.jpg")
    admin_client.post(PATH_URL, {"storage": "default", "prefix": "netzwerk/photos/",
                                 "mode": "steps", "confirm": "yes"})
    assert photo_path.exists("netzwerk/photos/a.jpg")
    assert read(photo_path, "netzwerk/photos/a.jpg") == "neu"   # juengste echte Version


def test_path_point_in_time(storage, admin_client):
    put(storage, "a-alt", "netzwerk/photos/a.jpg")
    put(storage, "b-alt", "netzwerk/photos/b.jpg")
    marker = djtz.localtime().replace(microsecond=0)
    put(storage, "a-neu", "netzwerk/photos/a.jpg")
    put(storage, "b-neu", "netzwerk/photos/b.jpg")

    resp = admin_client.post(PATH_URL, {
        "storage": "default", "prefix": "netzwerk/photos/", "mode": "at",
        "at": marker.strftime("%Y-%m-%dT%H:%M:%S"), "confirm": "yes"}, follow=True)
    assert read(storage, "netzwerk/photos/a.jpg") == "a-alt"
    assert read(storage, "netzwerk/photos/b.jpg") == "b-alt"
    assert "auf den Stand vom" in resp.content.decode()


def test_path_point_in_time_skips_younger_files(storage, admin_client):
    put(storage, "alt", "netzwerk/photos/a.jpg")
    marker = djtz.localtime().replace(microsecond=0)
    put(storage, "neu", "netzwerk/photos/a.jpg")
    put(storage, "spaeter", "netzwerk/photos/neu.jpg")          # gab es damals nicht

    body = admin_client.post(PATH_URL, {
        "storage": "default", "prefix": "netzwerk/photos/", "mode": "at",
        "at": marker.strftime("%Y-%m-%dT%H:%M:%S")}).content.decode()
    assert "Ja, 1 Datei(en) zuruecksetzen" in body
    assert "existierte zu diesem Zeitpunkt noch nicht" in body

    admin_client.post(PATH_URL, {
        "storage": "default", "prefix": "netzwerk/photos/", "mode": "at",
        "at": marker.strftime("%Y-%m-%dT%H:%M:%S"), "confirm": "yes"})
    assert read(storage, "netzwerk/photos/a.jpg") == "alt"
    assert read(storage, "netzwerk/photos/neu.jpg") == "spaeter"   # bleibt bestehen


def test_path_skips_single_version_files(storage, admin_client):
    put(storage, "einzig", "netzwerk/photos/a.jpg")
    body = admin_client.post(PATH_URL, {"storage": "default",
                                        "prefix": "netzwerk/photos/",
                                        "mode": "steps"}).content.decode()
    assert "nichts zurueckzusetzen" in body
    assert "es gibt nur eine Version" in body


def test_path_writes_log_entry_per_file(photo_path, admin_client):
    admin_client.post(PATH_URL, {"storage": "default", "prefix": "netzwerk/photos/",
                                 "mode": "steps", "confirm": "yes"})
    assert sorted(LogEntry.objects.values_list("object_id", flat=True)) == [
        "netzwerk/photos/2024/b.jpg", "netzwerk/photos/a.jpg"]


def test_path_preview_is_capped(photo_path, admin_client, monkeypatch):
    from s3restore import admin as admin_module
    monkeypatch.setattr(admin_module, "PATH_PREVIEW", 1)
    body = admin_client.post(PATH_URL, {"storage": "default",
                                        "prefix": "netzwerk/photos/",
                                        "mode": "steps"}).content.decode()
    assert "und 1 weitere" in body


def test_path_rejects_bad_timestamp(photo_path, admin_client):
    resp = admin_client.post(PATH_URL, {"storage": "default",
                                        "prefix": "netzwerk/photos/",
                                        "mode": "at", "at": "gestern"}, follow=True)
    assert "kein gueltiger Zeitpunkt" in resp.content.decode()
    assert read(photo_path, "netzwerk/photos/a.jpg") == "neu"


def test_path_without_prefix_does_nothing(photo_path, admin_client):
    resp = admin_client.post(PATH_URL, {"storage": "default", "prefix": "",
                                        "mode": "steps", "confirm": "yes"}, follow=True)
    assert "Bitte erst einen Praefix angeben" in resp.content.decode()
    assert read(photo_path, "netzwerk/photos/a.jpg") == "neu"


def test_path_rejects_get_and_unknown_mode(photo_path, admin_client):
    assert admin_client.get(PATH_URL).status_code == 400
    assert admin_client.post(PATH_URL, {"storage": "default", "prefix": "netzwerk/",
                                        "mode": "loeschen"}).status_code == 400


def test_path_needs_restore_permission(photo_path, client, db):
    user = User.objects.create_user("leser3", password="pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_s3version"))
    client.force_login(user)
    body = client.get(CHANGELIST, {"prefix": "netzwerk/photos/"}).content.decode()
    assert "Ganzen Pfad wiederherstellen" not in body
    assert client.post(PATH_URL, {"storage": "default", "prefix": "netzwerk/photos/",
                                  "mode": "steps", "confirm": "yes"}).status_code == 403
    assert read(photo_path, "netzwerk/photos/a.jpg") == "neu"


# ------------------------- Dateien mit nur einer Version aus der Liste filtern

def test_single_version_files_are_hidden(storage, admin_client):
    put(storage, "alt", "config/zwei.json"); put(storage, "neu", "config/zwei.json")
    put(storage, "einzig", "config/eine.json")
    body = flat(admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode())
    assert "config/zwei.json" in body
    assert "config/eine.json" not in body
    assert "1 Objekt mit nur einer Version ausgeblendet" in body
    assert "Nur geloeschte" in body          # Filterleiste


def test_show_all_reveals_them_without_checkbox(storage, admin_client):
    put(storage, "alt", "config/zwei.json"); put(storage, "neu", "config/zwei.json")
    put(storage, "einzig", "config/eine.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/", "show": "all"}).content.decode()
    assert "config/eine.json" in body
    assert body.count('name="names"') == 1          # nur die mit Historie waehlbar


def test_deleted_file_with_one_version_stays_visible(storage, admin_client):
    """Geloescht + eine echte Version = wiederherstellbar, also nicht ausblenden."""
    put(storage, "v1", "config/weg.json")
    storage.delete("config/weg.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    assert "config/weg.json" in body and "geloescht" in body
    assert body.count('name="names"') == 1


def test_page_with_only_single_version_files_keeps_navigation(storage, admin_client):
    put(storage, "einzig", "config/eine.json")
    body = flat(admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode())
    assert "kein Objekt eine aeltere Version" in body
    assert "Keine Objekte unter diesem Praefix gefunden" not in body


def test_filter_survives_paging_links(storage, admin_client):
    for i in range(60):
        storage.s3_client.put_object(Bucket=BUCKET,
                                     Key=storage.key_for(f"config/d{i:03d}.txt"), Body=b"x")
    body = admin_client.get(CHANGELIST, {"prefix": "config/", "show": "all"}).content.decode()
    assert "show=all" in next_url(body)


# ------------------------------------------------- Filter "nur geloeschte"

def test_filter_bar_is_rendered(storage, admin_client):
    put(storage, "v1", "config/a.json"); put(storage, "v2", "config/a.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/"}).content.decode()
    for label in ("Wiederherstellbar", "Nur geloeschte", "Alle"):
        assert label in body
    assert "show=deleted" in body and "show=all" in body


def test_deleted_filter_shows_only_deleted(storage, admin_client):
    put(storage, "lebt", "config/a.json"); put(storage, "lebt2", "config/a.json")
    put(storage, "weg", "config/b.json"); put(storage, "weg2", "config/b.json")
    storage.delete("config/b.json")

    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert "config/b.json" in body and "config/a.json" not in body
    assert body.count('name="names"') == 1          # bleibt wiederherstellbar
    assert "geloescht" in body


def test_deleted_filter_finds_entries_beyond_first_page(many_files, admin_client):
    """Der Filter sucht vorwaerts weiter, statt nur die aktuelle Seite zu sieben."""
    many_files.delete("config/datei-099.txt")
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert "config/datei-099.txt" in body
    assert body.count('name="names"') == 1
    assert "120 Objekte dafuer durchsucht" in flat(body)


def test_deleted_filter_skips_objects_without_real_version(storage, admin_client):
    """Nur ein Delete-Marker und sonst nichts -> nichts zurueckzuholen."""
    put(storage, "v1", "config/a.json")
    storage.delete("config/a.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert "config/a.json" in body                  # eine echte Version ist da

    versions = storage.versions("config/a.json")
    real = [v for v in versions if not v.is_delete_marker][0]
    storage.s3_client.delete_object(Bucket=BUCKET, Key=storage.key_for("config/a.json"),
                                    VersionId=real.version_id)
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert "config/a.json" not in body
    assert "Keine geloeschten Dateien gefunden" in body


def test_deleted_filter_reports_scan_cap(many_files, admin_client, monkeypatch):
    from s3restore import admin as admin_module
    monkeypatch.setattr(admin_module, "SCAN_LIMIT", 10)
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert "danach abgebrochen" in flat(body)
    assert next_url(body)                            # Weitersuchen ab dieser Stelle


def test_deleted_filter_survives_paging_and_bulk(storage, admin_client):
    for i in range(3):
        name = f"config/weg-{i}.json"
        put(storage, "alt", name); put(storage, "neu", name)
        storage.delete(name)
    body = admin_client.get(CHANGELIST, {"prefix": "config/",
                                         "show": "deleted"}).content.decode()
    assert 'name="show" value="deleted"' in body

    admin_client.post(BULK, {"storage": "default", "prefix": "config/",
                             "show": "deleted", "action": "restore_previous",
                             "confirm": "yes",
                             "names": ["config/weg-0.json", "config/weg-1.json"]})
    assert storage.exists("config/weg-0.json") and storage.exists("config/weg-1.json")
    assert not storage.exists("config/weg-2.json")


def test_old_all_parameter_still_works(storage, admin_client):
    put(storage, "einzig", "config/eine.json")
    body = admin_client.get(CHANGELIST, {"prefix": "config/", "all": "1"}).content.decode()
    assert "config/eine.json" in body
