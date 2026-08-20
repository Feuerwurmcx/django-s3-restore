"""Tests fuer die Admin-Seite zur S3-Wiederherstellung (moto, kein echtes S3)."""
import time
from urllib.parse import urlencode

import boto3
import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.urls import reverse
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
    assert body.count('name="names"') == 3
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
