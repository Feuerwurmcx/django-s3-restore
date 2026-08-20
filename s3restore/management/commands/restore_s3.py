"""
manage.py restore_s3 -- vorherige Version(en) aus dem S3-Bucket zurueckholen.

Arbeitet auf dem konfigurierten Django-Storage (VersionedS3Storage), nicht auf
einer eigenen boto3-Session: Bucket, Credentials und der `location`-Praefix
kommen aus settings.STORAGES. Namen werden deshalb Django-relativ angegeben
(z.B. "config/zones.json"), nicht als voller S3-Key.

Beispiele
---------
    # Historie ansehen
    python manage.py restore_s3 config/zones.json --list

    # eine Version zurueck (Standard), mit Rueckfrage
    python manage.py restore_s3 config/zones.json

    # drei Versionen zurueck, ohne Rueckfrage
    python manage.py restore_s3 config/zones.json --steps 3 --noinput

    # exakte Version
    python manage.py restore_s3 config/zones.json --version-id 3sL9v.Kx0dQe7pM1

    # ganzes Verzeichnis auf den Stand von gestern 18:00 (Probelauf)
    python manage.py restore_s3 config/ --prefix --at 2026-08-18T18:00 --dry-run

    # anderer Storage-Alias aus STORAGES
    python manage.py restore_s3 backups/db.sqlite3 --storage backups
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

try:  # Django >= 4.2
    from django.core.files.storage import storages

    def get_storage(alias: str):
        return storages[alias]
except ImportError:  # pragma: no cover - aeltere Django-Versionen
    from django.core.files.storage import default_storage, get_storage_class

    def get_storage(alias: str):
        if alias != "default":
            raise CommandError("--storage braucht Django >= 4.2 (STORAGES-Setting)")
        return default_storage

from s3restore.storage import RestoreError, VersionedS3Storage


class Command(BaseCommand):
    help = "Stellt eine vorherige Version eines Objekts im S3-Bucket wieder her."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help="Django-relativer Name, z.B. config/zones.json "
                 "(mit --prefix: Verzeichnis-Praefix, '' = alles)",
        )
        parser.add_argument("--prefix", action="store_true",
                            help="name als Praefix behandeln, alle Objekte darunter")

        sel = parser.add_mutually_exclusive_group()
        sel.add_argument("--steps", type=int, metavar="N",
                         help="N Versionen zurueck (Standard: 1)")
        sel.add_argument("--version-id", help="exakte VersionId (nicht mit --prefix)")
        sel.add_argument("--at", metavar="ZEIT",
                         help="Stand zu diesem Zeitpunkt, ISO-Format, "
                              "z.B. 2026-08-18T18:00 (lokale Zeit)")

        parser.add_argument("--list", action="store_true", dest="list_only",
                            help="nur Versionshistorie anzeigen, nichts aendern")
        parser.add_argument("--dry-run", action="store_true",
                            help="nur zeigen, was passieren wuerde")
        parser.add_argument("--restore-deletes", action="store_true",
                            help="mit --at: war das Objekt damals geloescht, "
                                 "wird ein neuer Delete-Marker gesetzt")
        parser.add_argument("--noinput", "--no-input", action="store_false",
                            dest="interactive", help="ohne Rueckfrage ausfuehren")
        parser.add_argument("--storage", default="default",
                            help="Alias aus settings.STORAGES (Standard: default)")

    # ------------------------------------------------------------------ handle
    def handle(self, *args, **opts):
        storage = get_storage(opts["storage"])
        if not isinstance(storage, VersionedS3Storage):
            raise CommandError(
                f"Storage '{opts['storage']}' ist {type(storage).__name__}, "
                "erwartet wird s3restore.storage.VersionedS3Storage.")

        name = opts["name"]
        at = self._parse_at(opts["at"]) if opts["at"] else None
        if opts["version_id"] and opts["prefix"]:
            raise CommandError("--version-id geht nur fuer ein einzelnes Objekt")

        try:
            if opts["list_only"]:
                return self._show_history(storage, name, opts["prefix"])
            results = self._plan(storage, name, opts, at, dry_run=True)
        except RestoreError as exc:
            raise CommandError(str(exc)) from exc

        todo = [r for r in results if r.action in ("restored", "delete_marker")]
        for r in results:
            if r.action == "skipped":
                self.stdout.write(self.style.WARNING(f"  uebersprungen: {r}"))
        if not todo:
            self.stdout.write("Nichts zu tun.")
            return

        self.stdout.write(f"\nRollback in s3://{storage.bucket_name} "
                          f"({len(todo)} Objekt(e)):")
        for r in todo:
            self.stdout.write(f"  {r}")

        if opts["dry_run"]:
            self.stdout.write(self.style.NOTICE("\n--dry-run: nichts geaendert."))
            return

        if opts["interactive"]:
            answer = input("\nAusfuehren? [j/N] ").strip().lower()
            if answer not in ("j", "y", "ja", "yes"):
                raise CommandError("Abgebrochen.")

        try:
            results = self._plan(storage, name, opts, at, dry_run=False)
        except RestoreError as exc:
            raise CommandError(str(exc)) from exc

        failed = 0
        for r in results:
            if r.action == "failed":
                failed += 1
                self.stderr.write(self.style.ERROR(f"FEHLER  {r}"))
            elif r.changed:
                self.stdout.write(self.style.SUCCESS(f"OK  {r}"))

        done = sum(1 for r in results if r.changed)
        self.stdout.write(f"\nFertig: {done} wiederhergestellt, {failed} fehlgeschlagen.")
        if failed:
            raise CommandError("Nicht alle Objekte konnten zurueckgerollt werden.")

    # ------------------------------------------------------------------ helper
    def _plan(self, storage, name, opts, at, *, dry_run):
        common = dict(steps=opts["steps"], at=at,
                      restore_deletes=opts["restore_deletes"], dry_run=dry_run)
        if opts["prefix"]:
            return storage.restore_all(name, **common)
        return [storage.restore(name, version_id=opts["version_id"], **common)]

    def _show_history(self, storage, name, is_prefix):
        grouped = (storage.versions_under(name) if is_prefix
                   else {name: storage.versions(name)})
        empty = True
        for key, hist in sorted(grouped.items()):
            if not hist:
                continue
            empty = False
            self.stdout.write(f"\n{key}   ({len(hist)} Eintraege, * = aktuell)")
            for v in hist:
                self.stdout.write(f"   {v}")
        if empty:
            raise CommandError(f"nichts gefunden unter '{name}'")

    @staticmethod
    def _parse_at(value: str):
        dt = parse_datetime(value)
        if dt is None:
            raise CommandError(
                f"'{value}' ist kein ISO-Zeitstempel (z.B. 2026-08-18T18:00:00)")
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
