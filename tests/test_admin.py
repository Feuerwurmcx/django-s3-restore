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
