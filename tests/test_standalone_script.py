"""Tests fuer s3_restore.py gegen einen gemockten S3-Bucket (moto)."""
import time
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

import s3_restore

BUCKET = "garten-backup"
KEY = "config/zones.json"


def body(s3, key=KEY):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="eu-central-1")
        c.create_bucket(Bucket=BUCKET,
                        CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        c.put_bucket_versioning(Bucket=BUCKET,
                                VersioningConfiguration={"Status": "Enabled"})
        yield c


def put(s3, text, key=KEY):
    time.sleep(1.05)  # eindeutige LastModified-Sekunden
    return s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode())["VersionId"]


def test_one_step_back(s3, capsys):
    put(s3, "v1"); put(s3, "v2"); put(s3, "v3")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--yes"]) == 0
    assert body(s3) == "v2"
    # Historie ist gewachsen, nichts verloren
    hist = s3_restore.list_versions(s3, BUCKET, KEY)
    assert len(hist) == 4


def test_three_steps_back(s3):
    put(s3, "v1"); put(s3, "v2"); put(s3, "v3"); put(s3, "v4")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--steps", "3", "--yes"]) == 0
    assert body(s3) == "v1"


def test_too_far_back_is_skipped(s3):
    put(s3, "v1"); put(s3, "v2")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--steps", "9", "--yes"]) == 0
    assert body(s3) == "v2"  # unveraendert


def test_restore_after_delete_marker(s3):
    put(s3, "v1"); put(s3, "v2")
    s3.delete_object(Bucket=BUCKET, Key=KEY)
    with pytest.raises(Exception):
        body(s3)
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--yes"]) == 0
    assert body(s3) == "v2"  # juengste echte Version ist wieder da


def test_explicit_version_id(s3):
    vid1 = put(s3, "v1"); put(s3, "v2")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY,
                            "--version-id", vid1, "--yes"]) == 0
    assert body(s3) == "v1"


def test_point_in_time(s3):
    put(s3, "v1")
    marker = datetime.now(timezone.utc)
    put(s3, "v2"); put(s3, "v3")
    stamp = marker.astimezone().replace(microsecond=0).isoformat()
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--at", stamp, "--yes"]) == 0
    assert body(s3) == "v1"


def test_point_in_time_before_object_existed(s3):
    put(s3, "v1")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY,
                            "--at", "2020-01-01T00:00:00", "--yes"]) == 0
    assert body(s3) == "v1"


def test_at_hits_delete_marker_with_restore_deletes(s3):
    put(s3, "v1")
    s3.delete_object(Bucket=BUCKET, Key=KEY)
    time.sleep(1.05)
    marker = datetime.now(timezone.utc)
    put(s3, "v2")
    stamp = marker.astimezone().replace(microsecond=0).isoformat()
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--at", stamp,
                            "--restore-deletes", "--yes"]) == 0
    with pytest.raises(Exception):
        body(s3)  # wieder im geloeschten Zustand


def test_prefix_rolls_back_all_keys(s3):
    put(s3, "a1", "config/a.json"); put(s3, "a2", "config/a.json")
    put(s3, "b1", "config/b.json"); put(s3, "b2", "config/b.json")
    put(s3, "other1", "logs/x.txt"); put(s3, "other2", "logs/x.txt")
    assert s3_restore.main(["--bucket", BUCKET, "--prefix", "config/", "--yes"]) == 0
    assert body(s3, "config/a.json") == "a1"
    assert body(s3, "config/b.json") == "b1"
    assert body(s3, "logs/x.txt") == "other2"  # nicht angefasst


def test_key_is_exact_not_prefix(s3):
    put(s3, "x1", "config/zones.json"); put(s3, "x2", "config/zones.json")
    put(s3, "y1", "config/zones.json.bak"); put(s3, "y2", "config/zones.json.bak")
    assert s3_restore.main(["--bucket", BUCKET, "--key", "config/zones.json", "--yes"]) == 0
    assert body(s3, "config/zones.json") == "x1"
    assert body(s3, "config/zones.json.bak") == "y2"


def test_dry_run_changes_nothing(s3, capsys):
    put(s3, "v1"); put(s3, "v2")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--dry-run"]) == 0
    assert body(s3) == "v2"
    assert "dry-run" in capsys.readouterr().out


def test_list_only(s3, capsys):
    put(s3, "v1"); put(s3, "v2")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--list"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n   ") >= 2 and "*" in out
    assert body(s3) == "v2"


def test_metadata_and_content_type_preserved(s3):
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"v1", ContentType="application/json",
                  Metadata={"zone": "beet-nord"})
    time.sleep(1.05)
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"v2", ContentType="text/plain")
    assert s3_restore.main(["--bucket", BUCKET, "--key", KEY, "--yes"]) == 0
    head = s3.head_object(Bucket=BUCKET, Key=KEY)
    assert head["ContentType"] == "application/json"
    assert head["Metadata"]["zone"] == "beet-nord"


def test_unversioned_bucket_is_rejected(capsys):
    with mock_aws():
        c = boto3.client("s3", region_name="eu-central-1")
        c.create_bucket(Bucket="plain",
                        CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        c.put_object(Bucket="plain", Key="a.txt", Body=b"x")
        assert s3_restore.main(["--bucket", "plain", "--key", "a.txt", "--yes"]) == 1
        assert "Versioning" in capsys.readouterr().err


def test_missing_key(s3, capsys):
    assert s3_restore.main(["--bucket", BUCKET, "--key", "gibts/nicht", "--yes"]) == 1
    assert "nichts gefunden" in capsys.readouterr().err
