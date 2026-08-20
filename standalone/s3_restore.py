#!/usr/bin/env python3
"""
s3_restore.py -- Wiederherstellung einer vorherigen Objektversion in einem
versionierten S3-Bucket.

Das Script rollt im Bucket zurueck: die gewaehlte alte Version wird per
copy_object erneut geschrieben und damit zur neuen aktuellen Version.
Es wird nichts geloescht -- die Historie bleibt vollstaendig erhalten.

Beispiele
---------
  # Historie eines Objekts ansehen
  ./s3_restore.py --bucket garten-backup --key config/zones.json --list

  # eine Version zurueck (Standard)
  ./s3_restore.py --bucket garten-backup --key config/zones.json

  # drei Versionen zurueck, ohne Rueckfrage
  ./s3_restore.py --bucket garten-backup --key config/zones.json --steps 3 --yes

  # exakte Version
  ./s3_restore.py --bucket garten-backup --key config/zones.json \
      --version-id 3sL9v.Kx0dQe7pM1

  # kompletten Praefix auf den Stand von gestern 18:00 Uhr bringen (Probelauf)
  ./s3_restore.py --bucket garten-backup --prefix config/ \
      --at 2026-08-18T18:00:00 --dry-run

Voraussetzungen: Python 3.9+, boto3, Bucket mit aktiviertem Versioning.
Benoetigte Rechte: s3:ListBucketVersions, s3:GetObjectVersion, s3:PutObject.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    sys.exit("boto3 fehlt -- installieren mit:  pip install boto3")


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------

@dataclass
class Version:
    """Eine Version oder ein Delete-Marker eines Objekts."""
    key: str
    version_id: str
    last_modified: datetime
    is_latest: bool
    is_delete_marker: bool
    size: int | None = None
    etag: str | None = None
    storage_class: str | None = None

    @property
    def label(self) -> str:
        return "DELETE-MARKER" if self.is_delete_marker else f"{self.size or 0:>10} B"

    def row(self) -> str:
        mark = "*" if self.is_latest else " "
        ts = self.last_modified.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        return f"{mark} {ts}  {self.label:>16}  {self.version_id}"


class RestoreError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# S3-Zugriff
# --------------------------------------------------------------------------

def make_client(profile: str | None, region: str | None, endpoint_url: str | None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3", region_name=region, endpoint_url=endpoint_url)


def check_versioning(s3, bucket: str) -> None:
    """Bricht ab, wenn der Bucket kein Versioning hat -- sonst waere ein
    Rollback gar nicht moeglich und wir wuerden stillschweigend Unsinn tun."""
    try:
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    except ClientError as exc:
        raise RestoreError(f"Bucket '{bucket}' nicht lesbar: {exc}") from exc
    if status not in ("Enabled", "Suspended"):
        raise RestoreError(
            f"Bucket '{bucket}' hat kein Object-Versioning aktiviert "
            "-- es gibt keine vorherigen Versionen zum Wiederherstellen."
        )
    if status == "Suspended":
        print(f"Hinweis: Versioning auf '{bucket}' ist SUSPENDED. "
              "Alte Versionen sind noch da, neue entstehen aber nicht mehr.",
              file=sys.stderr)


def list_versions(s3, bucket: str, prefix: str) -> list[Version]:
    """Alle Versionen + Delete-Marker unter einem Praefix, neueste zuerst."""
    out: list[Version] = []
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            out.append(Version(
                key=v["Key"], version_id=v["VersionId"],
                last_modified=v["LastModified"], is_latest=v["IsLatest"],
                is_delete_marker=False, size=v.get("Size"),
                etag=v.get("ETag"), storage_class=v.get("StorageClass"),
            ))
        for d in page.get("DeleteMarkers", []):
            out.append(Version(
                key=d["Key"], version_id=d["VersionId"],
                last_modified=d["LastModified"], is_latest=d["IsLatest"],
                is_delete_marker=True,
            ))
    out.sort(key=_sort_key, reverse=True)
    return out


def _sort_key(v: Version):
    """Neueste zuerst. S3-Zeitstempel loesen nur auf Sekunden auf; bei
    Gleichstand gewinnt die als aktuell markierte Version, danach der
    Delete-Marker (der kann nur nach einem Schreibvorgang entstanden sein),
    zuletzt die VersionId als stabiler Tiebreaker."""
    return (v.last_modified, v.is_latest, v.is_delete_marker, v.version_id)


def group_by_key(versions: Iterable[Version]) -> dict[str, list[Version]]:
    grouped: dict[str, list[Version]] = {}
    for v in versions:
        grouped.setdefault(v.key, []).append(v)
    for hist in grouped.values():
        hist.sort(key=_sort_key, reverse=True)
    return grouped


# --------------------------------------------------------------------------
# Auswahl der Zielversion
# --------------------------------------------------------------------------

def pick_version(history: list[Version], *, steps: int | None,
                 version_id: str | None, at: datetime | None) -> Version:
    """Waehlt aus der (absteigend sortierten) Historie die Zielversion."""
    if not history:
        raise RestoreError("keine Versionen vorhanden")

    if version_id:
        for v in history:
            if v.version_id == version_id:
                return v
        raise RestoreError(f"VersionId '{version_id}' existiert fuer diesen Key nicht")

    if at is not None:
        candidates = [v for v in history if v.last_modified <= at]
        if not candidates:
            raise RestoreError(
                f"zum Zeitpunkt {at.astimezone():%Y-%m-%d %H:%M:%S} existierte das Objekt noch nicht")
        return candidates[0]

    # --steps N: N echte Versionen zurueck, Delete-Marker werden uebersprungen.
    real = [v for v in history if not v.is_delete_marker]
    if not real:
        raise RestoreError("nur Delete-Marker vorhanden, keine wiederherstellbaren Daten")
    idx = steps if steps is not None else 1
    # Ist die aktuelle Spitze ein Delete-Marker, ist die juengste echte Version
    # bereits "eine Version zurueck" -- dann nicht zusaetzlich abziehen.
    current = next((v for v in history if v.is_latest), history[0])
    if current.is_delete_marker:
        idx -= 1
    if idx >= len(real):
        raise RestoreError(
            f"nur {len(real)} Version(en) vorhanden, {idx} Schritt(e) zurueck geht nicht")
    return real[max(idx, 0)]


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------

def rollback(s3, bucket: str, target: Version, *, dry_run: bool) -> str:
    """Kopiert die Zielversion auf sich selbst -> sie wird neue aktuelle Version."""
    if dry_run:
        return "dry-run"
    try:
        resp = s3.copy_object(
            Bucket=bucket,
            Key=target.key,
            CopySource={"Bucket": bucket, "Key": target.key,
                        "VersionId": target.version_id},
            MetadataDirective="COPY",
            TaggingDirective="COPY",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("InvalidObjectState", "ObjectNotInActiveTierError"):
            raise RestoreError(
                f"{target.key}: Version liegt in Glacier/Deep Archive und muss erst "
                "per restore_object wieder verfuegbar gemacht werden") from exc
        raise RestoreError(f"{target.key}: Kopieren fehlgeschlagen -- {exc}") from exc
    return resp.get("VersionId", "?")


def apply_delete_marker(s3, bucket: str, key: str, *, dry_run: bool) -> str:
    """Zustand 'geloescht' wiederherstellen: neuen Delete-Marker setzen."""
    if dry_run:
        return "dry-run"
    resp = s3.delete_object(Bucket=bucket, Key=key)
    return resp.get("VersionId", "?")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' ist kein ISO-Zeitstempel (z.B. 2026-08-18T18:00:00)")
    if dt.tzinfo is None:                      # naiv -> lokale Zeitzone annehmen
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Vorherige Version(en) in einem versionierten S3-Bucket wiederherstellen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Rollback = die alte Version wird erneut hochgeladen und damit aktuell. "
               "Es wird nie etwas endgueltig geloescht.",
    )
    p.add_argument("--bucket", required=True, help="Name des S3-Buckets")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--key", help="genau ein Objekt")
    what.add_argument("--prefix", help="alle Objekte unter diesem Praefix")

    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--steps", type=int, metavar="N",
                     help="N Versionen zurueck (Standard: 1)")
    sel.add_argument("--version-id", help="exakte VersionId (nur mit --key sinnvoll)")
    sel.add_argument("--at", type=parse_time, metavar="ZEIT",
                     help="Stand zu diesem Zeitpunkt, ISO-Format, "
                          "z.B. 2026-08-18T18:00:00 (lokale Zeit)")

    p.add_argument("--list", action="store_true", dest="list_only",
                   help="nur Versionshistorie anzeigen, nichts aendern")
    p.add_argument("--dry-run", action="store_true", help="nur zeigen, was passieren wuerde")
    p.add_argument("-y", "--yes", action="store_true", help="ohne Rueckfrage ausfuehren")
    p.add_argument("--restore-deletes", action="store_true",
                   help="mit --at: war das Objekt zum Zeitpunkt geloescht, "
                        "wird ein neuer Delete-Marker gesetzt (statt zu ueberspringen)")
    p.add_argument("--profile", help="AWS-Profil")
    p.add_argument("--region", help="AWS-Region")
    p.add_argument("--endpoint-url", help="alternativer Endpoint (MinIO, Ceph, ...)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prefix = args.key if args.key else args.prefix

    if args.version_id and not args.key:
        return fail("--version-id funktioniert nur zusammen mit --key")

    try:
        s3 = make_client(args.profile, args.region, args.endpoint_url)
        check_versioning(s3, args.bucket)
        all_versions = list_versions(s3, args.bucket, prefix)
    except (RestoreError, NoCredentialsError, ClientError) as exc:
        return fail(str(exc))

    grouped = group_by_key(all_versions)
    if args.key:                      # exakter Treffer, kein Praefix-Match
        grouped = {k: v for k, v in grouped.items() if k == args.key}
    if not grouped:
        return fail(f"nichts gefunden unter '{prefix}' in Bucket '{args.bucket}'")

    # --- nur anzeigen -----------------------------------------------------
    if args.list_only:
        for key, hist in sorted(grouped.items()):
            print(f"\n{key}   ({len(hist)} Eintraege, * = aktuell)")
            for v in hist:
                print("   " + v.row())
        return 0

    # --- Plan aufbauen ----------------------------------------------------
    plan: list[tuple[str, Version | None]] = []
    problems: list[str] = []
    for key, hist in sorted(grouped.items()):
        try:
            target = pick_version(hist, steps=args.steps,
                                  version_id=args.version_id, at=args.at)
        except RestoreError as exc:
            problems.append(f"  {key}: {exc}")
            continue
        if target.is_delete_marker:
            if args.restore_deletes:
                plan.append((key, None))          # None = Delete-Marker setzen
            else:
                problems.append(f"  {key}: war zu diesem Zeitpunkt geloescht "
                                "(uebersprungen, siehe --restore-deletes)")
            continue
        if target.is_latest:
            problems.append(f"  {key}: gewaehlte Version ist bereits die aktuelle")
            continue
        plan.append((key, target))

    if problems:
        print("Uebersprungen:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
    if not plan:
        print("Nichts zu tun.")
        return 0

    print(f"\nRollback in s3://{args.bucket} ({len(plan)} Objekt(e)):")
    for key, target in plan:
        if target is None:
            print(f"  {key}  ->  Delete-Marker setzen (Objekt war geloescht)")
        else:
            ts = target.last_modified.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {key}  ->  {target.version_id}  ({ts})")

    if args.dry_run:
        print("\n--dry-run: nichts geaendert.")
        return 0

    if not args.yes and sys.stdin.isatty():
        if input("\nAusfuehren? [j/N] ").strip().lower() not in ("j", "y", "ja", "yes"):
            print("Abgebrochen.")
            return 1

    # --- ausfuehren -------------------------------------------------------
    errors = 0
    for key, target in plan:
        try:
            if target is None:
                new_id = apply_delete_marker(s3, args.bucket, key, dry_run=False)
                print(f"OK  {key}  Delete-Marker {new_id}")
            else:
                new_id = rollback(s3, args.bucket, target, dry_run=False)
                print(f"OK  {key}  neue Version {new_id}")
        except (RestoreError, ClientError) as exc:
            errors += 1
            print(f"FEHLER  {exc}", file=sys.stderr)

    print(f"\nFertig: {len(plan) - errors} von {len(plan)} erfolgreich.")
    return 1 if errors else 0


def fail(msg: str) -> int:
    print(f"Fehler: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
