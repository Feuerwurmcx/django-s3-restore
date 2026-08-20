"""Tests fuer VersionedS3Storage und den restore_s3-Command (moto, kein echtes S3)."""
import io
import time
from datetime import datetime, timezone as dt_timezone

import boto3
import pytest
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from moto import mock_aws

from s3restore.storage import RestoreError, VersionedS3Storage

BUCKET = "garten-backup"
NAME = "config/zones.json"


@pytest.fixture
def storage():
    with mock_aws():
        client = boto3.client("s3", region_name="eu-central-1")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        # Storage-Instanz innerhalb des Mocks bauen; den Cache von
        # django.core.files.storage.storages leeren, damit der Command
        # dieselbe (gemockte) Instanz bekommt.
        from django.core.files.storage import storages
        storages._storages.clear()
        yield storages["default"]
        storages._storages.clear()


def put(storage, text, name=NAME):
    time.sleep(1.05)  # S3-Zeitstempel loesen nur auf Sekunden auf
    storage.save(name, ContentFile(text.encode()))


def read(storage, name=NAME):
    with storage.open(name) as fh:
        return fh.read().decode()


# ------------------------------------------------------------------ Grundlagen

def test_location_prefix_is_used(storage):
    put(storage, "v1")
    keys = [o["Key"] for o in storage.s3_client.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == ["media/config/zones.json"]
    assert storage.key_for(NAME) == "media/config/zones.json"
    assert storage.name_for("media/config/zones.json") == NAME


def test_versions_listing(storage):
    put(storage, "v1"); put(storage, "v2")
    hist = storage.versions(NAME)
    assert len(hist) == 2
    assert hist[0].is_latest and not hist[1].is_latest
    assert hist[0].name == NAME
    assert "*" in str(hist[0])


def test_versions_needs_versioning():
    with mock_aws():
        client = boto3.client("s3", region_name="eu-central-1")
        client.create_bucket(Bucket="plain",
                             CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        st = VersionedS3Storage(bucket_name="plain", region_name="eu-central-1")
        with pytest.raises(RestoreError, match="Versioning"):
            st.versions("a.txt")


# --------------------------------------------------------------------- restore

def test_one_step_back(storage):
    put(storage, "v1"); put(storage, "v2"); put(storage, "v3")
    result = storage.restore(NAME)
    assert result.action == "restored" and result.changed
    assert read(storage) == "v2"
    assert len(storage.versions(NAME)) == 4  # Historie bleibt vollstaendig


def test_three_steps_back(storage):
    for t in ("v1", "v2", "v3", "v4"):
        put(storage, t)
    assert storage.restore(NAME, steps=3).action == "restored"
    assert read(storage) == "v1"


def test_too_far_back_is_skipped(storage):
    put(storage, "v1"); put(storage, "v2")
    result = storage.restore(NAME, steps=9)
    assert result.action == "skipped" and "Schritt" in result.reason
    assert read(storage) == "v2"


def test_restore_after_delete(storage):
    put(storage, "v1"); put(storage, "v2")
    storage.delete(NAME)
    assert not storage.exists(NAME)
    assert storage.restore(NAME).action == "restored"
    assert read(storage) == "v2"  # juengste echte Version, nicht v1


def test_explicit_version_id(storage):
    put(storage, "v1"); put(storage, "v2")
    oldest = storage.versions(NAME)[-1]
    assert storage.restore(NAME, version_id=oldest.version_id).action == "restored"
    assert read(storage) == "v1"


def test_unknown_version_id_is_skipped(storage):
    put(storage, "v1")
    result = storage.restore(NAME, version_id="gibtsnicht")
    assert result.action == "skipped" and "VersionId" in result.reason


def test_point_in_time(storage):
    put(storage, "v1")
    marker = datetime.now(dt_timezone.utc)
    put(storage, "v2"); put(storage, "v3")
    assert storage.restore(NAME, at=marker).action == "restored"
    assert read(storage) == "v1"


def test_point_in_time_before_creation_is_skipped(storage):
    put(storage, "v1")
    result = storage.restore(NAME, at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc))
    assert result.action == "skipped" and "existierte" in result.reason


def test_at_hits_delete_marker(storage):
    put(storage, "v1")
    storage.delete(NAME)
    time.sleep(1.05)
    marker = datetime.now(dt_timezone.utc)
    put(storage, "v2")

    skipped = storage.restore(NAME, at=marker)
    assert skipped.action == "skipped" and "geloescht" in skipped.reason
    assert storage.exists(NAME)

    applied = storage.restore(NAME, at=marker, restore_deletes=True)
    assert applied.action == "delete_marker"
    assert not storage.exists(NAME)


def test_current_version_is_skipped(storage):
    put(storage, "v1")
    result = storage.restore(NAME, steps=0)
    assert result.action == "skipped" and "aktuelle" in result.reason


def test_missing_object(storage):
    result = storage.restore("gibts/nicht.json")
    assert result.action == "skipped" and "nicht im Bucket" in result.reason


def test_dry_run_changes_nothing(storage):
    put(storage, "v1"); put(storage, "v2")
    result = storage.restore(NAME, dry_run=True)
    assert result.action == "restored" and result.dry_run and not result.changed
    assert read(storage) == "v2"


def test_metadata_and_content_type_preserved(storage):
    storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for(NAME), Body=b"v1",
                                 ContentType="application/json",
                                 Metadata={"zone": "beet-nord"})
    time.sleep(1.05)
    storage.s3_client.put_object(Bucket=BUCKET, Key=storage.key_for(NAME), Body=b"v2",
                                 ContentType="text/plain")
    assert storage.restore(NAME).action == "restored"
    head = storage.s3_client.head_object(Bucket=BUCKET, Key=storage.key_for(NAME))
    assert head["ContentType"] == "application/json"
    assert head["Metadata"]["zone"] == "beet-nord"


# ---------------------------------------------------------------------- prefix

def test_restore_all_under_prefix(storage):
    for name in ("config/a.json", "config/b.json", "logs/x.txt"):
        put(storage, "alt", name); put(storage, "neu", name)
    results = storage.restore_all("config/")
    assert [r.action for r in results] == ["restored", "restored"]
    assert read(storage, "config/a.json") == "alt"
    assert read(storage, "config/b.json") == "alt"
    assert read(storage, "logs/x.txt") == "neu"  # nicht angefasst


def test_single_name_is_exact_not_prefix(storage):
    put(storage, "x1"); put(storage, "x2")
    put(storage, "y1", NAME + ".bak"); put(storage, "y2", NAME + ".bak")
    assert storage.restore(NAME).action == "restored"
    assert read(storage) == "x1"
    assert read(storage, NAME + ".bak") == "y2"


# --------------------------------------------------------------------- Command

def test_command_restores(storage, capsys):
    put(storage, "v1"); put(storage, "v2")
    call_command("restore_s3", NAME, interactive=False)
    out = capsys.readouterr().out
    assert "OK" in out and "Fertig: 1" in out
    assert read(storage) == "v1"


def test_command_dry_run(storage, capsys):
    put(storage, "v1"); put(storage, "v2")
    call_command("restore_s3", NAME, dry_run=True, interactive=False)
    assert "dry-run" in capsys.readouterr().out
    assert read(storage) == "v2"


def test_command_list(storage, capsys):
    put(storage, "v1"); put(storage, "v2")
    call_command("restore_s3", NAME, list_only=True)
    out = capsys.readouterr().out
    assert "2 Eintraege" in out and out.count("\n   ") >= 2
    assert read(storage) == "v2"


def test_command_prefix_with_at(storage, capsys):
    put(storage, "a1", "config/a.json")
    marker = datetime.now().astimezone()
    put(storage, "a2", "config/a.json")
    call_command("restore_s3", "config/", prefix=True, interactive=False,
                 at=marker.replace(microsecond=0).isoformat())
    assert read(storage, "config/a.json") == "a1"


def test_command_rejects_bad_timestamp(storage):
    with pytest.raises(CommandError, match="ISO-Zeitstempel"):
        call_command("restore_s3", NAME, at="gestern", interactive=False)


def test_command_rejects_version_id_with_prefix(storage):
    with pytest.raises(CommandError, match="einzelnes Objekt"):
        call_command("restore_s3", "config/", prefix=True, version_id="x",
                     interactive=False)


def test_command_nothing_to_do(storage, capsys):
    put(storage, "v1")
    call_command("restore_s3", NAME, interactive=False)
    assert "Nichts zu tun" in capsys.readouterr().out


def test_command_rejects_wrong_storage_backend(storage):
    from django.test import override_settings
    with override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }):
        from django.core.files.storage import storages
        storages._storages.clear()
        with pytest.raises(CommandError, match="VersionedS3Storage"):
            call_command("restore_s3", NAME, interactive=False)
