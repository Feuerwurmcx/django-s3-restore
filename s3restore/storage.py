"""
s3restore/storage.py

VersionedS3Storage -- eine S3Boto3Storage-Subklasse, die vorherige Versionen
eines Objekts auflisten und im Bucket zurueckrollen kann.

Es wird bewusst KEINE eigene boto3-Session gebaut: Client, Bucket-Name,
Credentials, Region und der `location`-Praefix kommen aus der Django-Storage-
Konfiguration (settings.STORAGES). Dadurch gilt in Django-Code, Management-
Commands und im Admin ueberall derselbe Zugang.

settings.py
-----------
    STORAGES = {
        "default": {
            "BACKEND": "s3restore.storage.VersionedS3Storage",
            "OPTIONS": {
                "bucket_name": "garten-backup",
                "region_name": "eu-central-1",
                "location": "media",       # Praefix im Bucket
                "file_overwrite": False,
            },
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

Der Bucket muss Object-Versioning aktiviert haben.
Benoetigte Rechte: s3:ListBucketVersions, s3:GetObjectVersion, s3:PutObject.

Benutzung
---------
    from django.core.files.storage import storages
    storage = storages["default"]

    for v in storage.versions("config/zones.json"):
        print(v.last_modified, v.version_id, v.size)

    result = storage.restore("config/zones.json", steps=1)
    print(result.action, result.new_version_id)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from botocore.exceptions import ClientError
from storages.backends.s3boto3 import S3Boto3Storage
from storages.utils import clean_name

__all__ = ["VersionedS3Storage", "Version", "RestoreResult", "RestoreError"]


class RestoreError(RuntimeError):
    """Wiederherstellung nicht moeglich (kein Kandidat, Glacier, fehlende Rechte, ...)."""


@dataclass(frozen=True)
class Version:
    """Eine Version oder ein Delete-Marker eines Objekts."""

    name: str                 # Django-relativer Name (ohne location-Praefix)
    key: str                  # vollstaendiger S3-Key
    version_id: str
    last_modified: datetime
    is_latest: bool
    is_delete_marker: bool
    size: int | None = None
    etag: str | None = None
    storage_class: str | None = None

    def __str__(self) -> str:
        mark = "*" if self.is_latest else " "
        what = "DELETE-MARKER" if self.is_delete_marker else f"{self.size or 0} B"
        return (f"{mark} {self.last_modified.astimezone():%Y-%m-%d %H:%M:%S}  "
                f"{what:>16}  {self.version_id}")


@dataclass
class RestoreResult:
    """Ergebnis pro Objekt -- auch fuer --dry-run und uebersprungene Dateien."""

    name: str
    action: str                       # restored | delete_marker | skipped | failed
    source: Version | None = None     # Version, auf die zurueckgerollt wurde
    new_version_id: str | None = None
    reason: str = ""
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return self.action in ("restored", "delete_marker") and not self.dry_run

    def __str__(self) -> str:
        if self.action == "restored":
            ts = f"{self.source.last_modified.astimezone():%Y-%m-%d %H:%M:%S}"
            tail = "(dry-run)" if self.dry_run else f"-> neue Version {self.new_version_id}"
            return f"{self.name}: zurueck auf {self.source.version_id} ({ts}) {tail}"
        if self.action == "delete_marker":
            tail = "(dry-run)" if self.dry_run else f"-> {self.new_version_id}"
            return f"{self.name}: Delete-Marker gesetzt {tail}"
        return f"{self.name}: {self.action} -- {self.reason}"


def _sort_key(v: Version):
    """Neueste zuerst.

    S3-Zeitstempel loesen nur auf Sekunden auf. Bei Gleichstand innerhalb
    einer Sekunde gilt: die als aktuell markierte Version gewinnt, danach der
    Delete-Marker (der kann nur nach einem Schreibvorgang entstanden sein),
    zuletzt die VersionId als stabiler Tiebreaker.
    """
    return (v.last_modified, v.is_latest, v.is_delete_marker, v.version_id)


class VersionedS3Storage(S3Boto3Storage):
    """S3Boto3Storage + Versionshistorie und Rollback."""

    # ------------------------------------------------------------------ intern
    @property
    def s3_client(self):
        """Low-Level-Client aus der bereits konfigurierten Storage-Session."""
        return self.connection.meta.client

    def key_for(self, name: str) -> str:
        """Django-Name -> vollstaendiger S3-Key (beruecksichtigt `location`)."""
        return self._normalize_name(clean_name(name))

    def name_for(self, key: str) -> str:
        """S3-Key -> Django-Name (Umkehrung von key_for)."""
        prefix = self.location.strip("/")
        if prefix and key.startswith(prefix + "/"):
            return key[len(prefix) + 1:]
        return key

    def _check_versioning(self) -> str:
        try:
            status = self.s3_client.get_bucket_versioning(
                Bucket=self.bucket_name).get("Status")
        except ClientError as exc:
            raise RestoreError(f"Bucket '{self.bucket_name}' nicht lesbar: {exc}") from exc
        if status not in ("Enabled", "Suspended"):
            raise RestoreError(
                f"Bucket '{self.bucket_name}' hat kein Object-Versioning aktiviert "
                "-- es gibt keine vorherigen Versionen zum Wiederherstellen.")
        return status

    # ------------------------------------------------------------------ lesen
    def versions(self, name: str) -> list[Version]:
        """Alle Versionen genau dieses Objekts, neueste zuerst."""
        key = self.key_for(name)
        return [v for v in self._list(key) if v.key == key]

    def versions_under(self, prefix: str = "") -> dict[str, list[Version]]:
        """Versionen aller Objekte unter einem Praefix, gruppiert nach Name."""
        grouped: dict[str, list[Version]] = {}
        for v in self._list(self.key_for(prefix) if prefix else self.location):
            grouped.setdefault(v.name, []).append(v)
        for hist in grouped.values():
            hist.sort(key=_sort_key, reverse=True)
        return grouped

    def page_under(self, prefix: str = "", *, key_marker: str | None = None,
                   limit: int = 50, batch_size: int | None = None
                   ) -> tuple[dict[str, list[Version]], str | None]:
        """Ein Batch Objekte ab `key_marker` -- echtes S3-Paging.

        S3 kennt keine Gesamtzahl und kein Rueckwaertsblaettern: die API
        liefert Keys ab einem Marker. Diese Methode holt so viele Antworten,
        bis `limit` + 1 verschiedene Keys beisammen sind, und gibt zurueck:

            ({name: [Version, ...]}, naechster_key_marker | None)

        Der +1. Key wird nur benutzt, um zu wissen, dass der `limit`-te Key
        vollstaendig eingelesen ist (die Versionen eines Keys koennen sich
        ueber mehrere Antworten verteilen) -- er landet nicht im Ergebnis.
        Der zurueckgegebene Marker ist der letzte Key dieser Seite; damit
        beginnt die naechste Seite direkt dahinter.
        """
        self._check_versioning()
        key_prefix = self.key_for(prefix) if prefix else self.location
        kwargs = {"Bucket": self.bucket_name, "Prefix": key_prefix,
                  "MaxKeys": batch_size or max(limit * 2, 100)}
        if key_marker:
            kwargs["KeyMarker"] = key_marker

        grouped: dict[str, list[Version]] = {}
        truncated = True
        while truncated and len(grouped) <= limit:
            response = self.s3_client.list_object_versions(**kwargs)
            for version in self._versions_in(response):
                # AWS beginnt hinter dem KeyMarker, manche Nachbauten (moto,
                # je nach Version auch MinIO/Ceph) liefern den Marker-Key selbst
                # noch mit -- sonst erschiene er auf zwei Seiten.
                if key_marker and version.key <= key_marker:
                    continue
                grouped.setdefault(version.name, []).append(version)
            truncated = response.get("IsTruncated", False)
            if truncated:
                kwargs["KeyMarker"] = response["NextKeyMarker"]
                if response.get("NextVersionIdMarker"):
                    kwargs["VersionIdMarker"] = response["NextVersionIdMarker"]
                else:
                    kwargs.pop("VersionIdMarker", None)

        names = list(grouped)
        next_marker = None
        if len(names) > limit:
            for extra in names[limit:]:
                del grouped[extra]
            next_marker = self.key_for(names[limit - 1])
        for hist in grouped.values():
            hist.sort(key=_sort_key, reverse=True)
        return grouped, next_marker

    def iter_under(self, prefix: str = "", *, batch: int = 200):
        """Alle Objekte unter einem Praefix, batchweise ueber den KeyMarker.

        Anders als versions_under() haelt das nie den ganzen Praefix im
        Speicher -- gedacht fuer Pfade mit sehr vielen Objekten.
        Liefert (name, history) je Objekt, Keys aufsteigend sortiert.
        """
        marker = None
        while True:
            grouped, marker = self.page_under(prefix, key_marker=marker, limit=batch)
            for name, history in grouped.items():
                yield name, history
            if not marker:
                return

    def _versions_in(self, response) -> list[Version]:
        """Versionen und Delete-Marker einer list_object_versions-Antwort."""
        out = []
        for v in response.get("Versions", []):
            out.append(Version(
                name=self.name_for(v["Key"]), key=v["Key"],
                version_id=v["VersionId"], last_modified=v["LastModified"],
                is_latest=v["IsLatest"], is_delete_marker=False,
                size=v.get("Size"), etag=v.get("ETag"),
                storage_class=v.get("StorageClass"),
            ))
        for d in response.get("DeleteMarkers", []):
            out.append(Version(
                name=self.name_for(d["Key"]), key=d["Key"],
                version_id=d["VersionId"], last_modified=d["LastModified"],
                is_latest=d["IsLatest"], is_delete_marker=True,
            ))
        return out

    def _list(self, key_prefix: str) -> list[Version]:
        self._check_versioning()
        out: list[Version] = []
        paginator = self.s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=key_prefix):
            out.extend(self._versions_in(page))
        out.sort(key=_sort_key, reverse=True)
        return out

    # --------------------------------------------------------------- auswaehlen
    @staticmethod
    def pick(history: list[Version], *, steps: int | None = 1,
             version_id: str | None = None, at: datetime | None = None) -> Version:
        """Waehlt aus der (absteigend sortierten) Historie die Zielversion."""
        if not history:
            raise RestoreError("keine Versionen vorhanden")

        if version_id:
            for v in history:
                if v.version_id == version_id:
                    return v
            raise RestoreError(f"VersionId '{version_id}' gibt es fuer dieses Objekt nicht")

        if at is not None:
            candidates = [v for v in history if v.last_modified <= at]
            if not candidates:
                raise RestoreError(
                    f"zum Zeitpunkt {at.astimezone():%Y-%m-%d %H:%M:%S} "
                    "existierte das Objekt noch nicht")
            return candidates[0]

        real = [v for v in history if not v.is_delete_marker]
        if not real:
            raise RestoreError("nur Delete-Marker vorhanden, keine Daten zum Zurueckholen")
        idx = 1 if steps is None else steps
        current = next((v for v in history if v.is_latest), history[0])
        if current.is_delete_marker:
            # Die juengste echte Version ist bereits "eine zurueck".
            idx -= 1
        if idx >= len(real):
            raise RestoreError(
                f"nur {len(real)} Version(en) vorhanden, {idx} Schritt(e) zurueck geht nicht")
        return real[max(idx, 0)]

    # ------------------------------------------------------------ wiederherstellen
    def restore(self, name: str, *, steps: int | None = 1,
                version_id: str | None = None, at: datetime | None = None,
                restore_deletes: bool = False, dry_run: bool = False) -> RestoreResult:
        """Rollt ein Objekt auf eine fruehere Version zurueck.

        Die alte Version wird per copy_object erneut geschrieben und damit zur
        neuen aktuellen Version -- die Historie bleibt vollstaendig erhalten.
        """
        history = self.versions(name)
        if not history:
            return RestoreResult(name, "skipped", reason="nicht im Bucket gefunden")
        return self._restore_from(name, history, steps=steps, version_id=version_id,
                                  at=at, restore_deletes=restore_deletes, dry_run=dry_run)

    def restore_all(self, prefix: str = "", *, steps: int | None = 1,
                    at: datetime | None = None, restore_deletes: bool = False,
                    dry_run: bool = False) -> list[RestoreResult]:
        """Wie restore(), aber fuer alle Objekte unter einem Praefix."""
        results = []
        for name, history in self.iter_under(prefix):
            results.append(self._restore_from(
                name, history, steps=steps, version_id=None, at=at,
                restore_deletes=restore_deletes, dry_run=dry_run))
        return results

    def _restore_from(self, name, history, *, steps, version_id, at,
                      restore_deletes, dry_run) -> RestoreResult:
        try:
            target = self.pick(history, steps=steps, version_id=version_id, at=at)
        except RestoreError as exc:
            return RestoreResult(name, "skipped", reason=str(exc))

        if target.is_delete_marker:
            if not restore_deletes:
                return RestoreResult(
                    name, "skipped",
                    reason="war zu diesem Zeitpunkt geloescht (restore_deletes=True setzen)")
            if dry_run:
                return RestoreResult(name, "delete_marker", dry_run=True)
            resp = self.s3_client.delete_object(Bucket=self.bucket_name, Key=target.key)
            return RestoreResult(name, "delete_marker",
                                 new_version_id=resp.get("VersionId"))

        if target.is_latest:
            return RestoreResult(name, "skipped",
                                 reason="gewaehlte Version ist bereits die aktuelle")

        if dry_run:
            return RestoreResult(name, "restored", source=target, dry_run=True)

        try:
            resp = self.s3_client.copy_object(
                Bucket=self.bucket_name,
                Key=target.key,
                CopySource={"Bucket": self.bucket_name, "Key": target.key,
                            "VersionId": target.version_id},
                MetadataDirective="COPY",   # Content-Type und Metadaten uebernehmen
                TaggingDirective="COPY",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("InvalidObjectState", "ObjectNotInActiveTierError"):
                reason = ("Version liegt in Glacier/Deep Archive und muss erst per "
                          "restore_object verfuegbar gemacht werden")
            else:
                reason = str(exc)
            return RestoreResult(name, "failed", source=target, reason=reason)

        return RestoreResult(name, "restored", source=target,
                             new_version_id=resp.get("VersionId"))
