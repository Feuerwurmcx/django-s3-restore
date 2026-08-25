"""
s3restore/admin.py

Admin-Seite zum Ansehen und Wiederherstellen frueherer S3-Objektversionen.

    Admin  ->  S3restore  ->  S3-Wiederherstellung

Ablauf: Praefix durchsuchen -> Objekt waehlen -> Versionshistorie ansehen
-> Version auswaehlen -> Bestaetigen -> Rollback. Jede Wiederherstellung
landet als LogEntry in der Admin-Historie.

Rechte
------
    s3restore.view_s3version      Historie ansehen
    s3restore.restore_s3version   zusaetzlich wiederherstellen

Superuser haben beides automatisch. Die Permission-Eintraege legt Django beim
naechsten `manage.py migrate` an (das Modell braucht keine Tabelle).

Der Rollback laeuft ausschliesslich per POST mit CSRF-Token und
Bestaetigungsseite -- ein GET aendert nie etwas.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.options import get_content_type_for_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html

from .models import S3Version
from .storage import RestoreError, VersionedS3Storage

try:  # Django >= 4.2
    from django.core.files.storage import storages

    def _get_storage(alias: str):
        return storages[alias]
except ImportError:  # pragma: no cover
    from django.core.files.storage import default_storage

    def _get_storage(alias: str):
        return default_storage


PAGE_SIZE = 50       # Objekte pro Seite (ein S3-Batch ab dem KeyMarker)
BULK_LIMIT = 200     # so viele Dateien nimmt die Sammelaktion hoechstens entgegen
PATH_PREVIEW = 200   # so viele Zeilen zeigt die Pfad-Bestaetigung konkret
SCAN_LIMIT = 5000    # so viele Objekte durchsucht der Geloescht-Filter je Seite
SESSION_KEY = "s3restore_markers"   # Kette der Seiten-Startmarker je Praefix
MARKER_STACK_LIMIT = 500            # so viele Seiten merkt sich die Session


def versioned_aliases() -> list[str]:
    """Alle STORAGES-Aliase, deren Backend ein VersionedS3Storage ist."""
    found = []
    for alias in getattr(settings, "STORAGES", {}):
        try:
            if isinstance(_get_storage(alias), VersionedS3Storage):
                found.append(alias)
        except Exception:  # falsch konfigurierter Alias soll die Seite nicht killen
            continue
    return found


@admin.register(S3Version)
class S3VersionAdmin(admin.ModelAdmin):
    """ModelAdmin ohne Datenbank -- alle Views sind selbst gebaut."""

    # ------------------------------------------------------------- Berechtigung
    def has_module_permission(self, request):
        return self._can_view(request)

    def has_view_permission(self, request, obj=None):
        return self._can_view(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @staticmethod
    def _can_view(request) -> bool:
        return request.user.is_active and (
            request.user.is_superuser or request.user.has_perm("s3restore.view_s3version"))

    @staticmethod
    def _can_restore(request) -> bool:
        return request.user.is_active and (
            request.user.is_superuser or request.user.has_perm("s3restore.restore_s3version"))

    # -------------------------------------------------------------------- URLs
    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path("versions/", self.admin_site.admin_view(self.versions_view),
                 name="%s_%s_versions" % info),
            path("restore/", self.admin_site.admin_view(self.restore_view),
                 name="%s_%s_restore" % info),
            path("bulk/", self.admin_site.admin_view(self.bulk_view),
                 name="%s_%s_bulk" % info),
            path("path/", self.admin_site.admin_view(self.path_view),
                 name="%s_%s_path" % info),
            *super().get_urls(),
        ]

    # ------------------------------------------------------------------- Views
    def changelist_view(self, request, extra_context=None):
        """Uebersicht: Objekte unter einem Praefix mit ihrem aktuellen Stand."""
        if not self._can_view(request):
            raise PermissionDenied

        aliases = versioned_aliases()
        alias = self._alias(request, aliases)
        prefix = request.GET.get("prefix", "").strip()

        marker = request.GET.get("marker", "").strip()
        show = self._show_mode(request)
        rows, next_marker, page_number, prev_marker = [], None, 1, None
        hidden, hidden_deleted, scanned, scan_capped = 0, 0, 0, False
        if alias:
            storage = _get_storage(alias)
            try:
                if show == "deleted":
                    # Geloeschtes ist selten -- hier wird vorwaerts gesucht, bis
                    # die Seite voll ist, statt fast leere Seiten zu zeigen.
                    rows, next_marker, scanned, scan_capped = self._scan_deleted(
                        storage, alias, prefix, marker)
                else:
                    rows, next_marker, hidden, hidden_deleted = self._page_rows(
                        storage, alias, prefix, marker, show)
            except RestoreError as exc:
                self.message_user(request, str(exc), messages.ERROR)

            page_number, prev_marker = self._track_page(request, alias, prefix, marker)

        context = {
            **self.admin_site.each_context(request),
            "title": "S3-Wiederherstellung",
            "opts": self.model._meta,
            "aliases": aliases,
            "alias": alias,
            "bucket": getattr(_get_storage(alias), "bucket_name", "") if alias else "",
            "prefix": prefix,
            "rows": rows,
            "page_size": PAGE_SIZE,
            "page_number": page_number,
            "marker": marker,
            "first_url": self._changelist_url(alias, prefix, None, show) if alias else "",
            "prev_url": (self._changelist_url(alias, prefix, prev_marker, show)
                         if prev_marker is not None else ""),
            "next_url": (self._changelist_url(alias, prefix, next_marker, show)
                         if next_marker else ""),
            "show": show,
            "hidden": hidden,
            "hidden_deleted": hidden_deleted,
            "scanned": scanned,
            "scan_capped": scan_capped,
            "filters": [
                {"key": key, "label": label, "active": show == key,
                 "url": self._changelist_url(alias, prefix, None, key)}
                for key, label in (("restorable", "Wiederherstellbar"),
                                   ("live", "Ohne geloeschte"),
                                   ("deleted", "Nur geloeschte"),
                                   ("all", "Alle"))
            ] if alias else [],
            "can_restore": self._can_restore(request),
            "bulk_url": reverse("admin:s3restore_s3version_bulk"),
            "path_url": reverse("admin:s3restore_s3version_path"),
            **(extra_context or {}),
        }
        return render(request, "admin/s3restore/s3version/change_list.html", context)

    def versions_view(self, request):
        """Versionshistorie eines Objekts, mit Auswahl zum Wiederherstellen."""
        if not self._can_view(request):
            raise PermissionDenied

        aliases = versioned_aliases()
        alias = self._alias(request, aliases)
        name = request.GET.get("name", "").strip()
        if not alias or not name:
            raise Http404("Kein Objekt angegeben.")

        storage = _get_storage(alias)
        try:
            history = storage.versions(name)
        except RestoreError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            history = []
        if not history:
            raise Http404(f"'{name}' hat keine Versionen in diesem Bucket.")

        rows = [{
            "version": v,
            "download": self._presigned_url(storage, v),
        } for v in history]

        context = {
            **self.admin_site.each_context(request),
            "title": name,
            "opts": self.model._meta,
            "alias": alias,
            "bucket": storage.bucket_name,
            "name": name,
            "rows": rows,
            "can_restore": self._can_restore(request),
            "restore_url": reverse("admin:s3restore_s3version_restore"),
            "changelist_url": self._changelist_url(alias, name.rsplit("/", 1)[0] if "/" in name else ""),
        }
        return render(request, "admin/s3restore/s3version/versions.html", context)

    def restore_view(self, request):
        """Bestaetigen und ausfuehren. Nur POST -- ein GET aendert nie etwas."""
        if request.method != "POST":
            raise SuspiciousOperation("Wiederherstellung nur per POST.")
        if not self._can_restore(request):
            raise PermissionDenied

        aliases = versioned_aliases()
        alias = self._alias(request, aliases, source=request.POST)
        name = request.POST.get("name", "").strip()
        version_id = request.POST.get("version_id", "").strip()
        if not alias or not name or not version_id:
            raise SuspiciousOperation("Unvollstaendige Anfrage.")

        storage = _get_storage(alias)
        target = next((v for v in storage.versions(name) if v.version_id == version_id), None)
        if target is None:
            raise Http404("Diese Version gibt es nicht (mehr).")

        # Schritt 1: Bestaetigungsseite
        if request.POST.get("confirm") != "yes":
            context = {
                **self.admin_site.each_context(request),
                "title": "Version wiederherstellen?",
                "opts": self.model._meta,
                "alias": alias,
                "bucket": storage.bucket_name,
                "name": name,
                "target": target,
                "restore_url": reverse("admin:s3restore_s3version_restore"),
                "back_url": self._versions_url(alias, name),
            }
            return render(request, "admin/s3restore/s3version/restore_confirmation.html",
                          context)

        # Schritt 2: ausfuehren
        result = storage.restore(name, version_id=version_id)
        if result.action == "restored":
            self._log(request, alias, name, target)
            self.message_user(
                request,
                format_html(
                    "<b>{}</b> wurde auf den Stand vom {} zurueckgesetzt "
                    "(neue Version {}).",
                    name,
                    timezone.localtime(target.last_modified).strftime("%d.%m.%Y %H:%M:%S"),
                    result.new_version_id or "?"),
                messages.SUCCESS)
        elif result.action == "skipped":
            self.message_user(request, f"Nichts geaendert: {result.reason}", messages.WARNING)
        else:
            self.message_user(request, f"Fehlgeschlagen: {result.reason}", messages.ERROR)

        return HttpResponseRedirect(self._versions_url(alias, name))

    def bulk_view(self, request):
        """Sammelaktion aus der Uebersicht: mehrere Dateien eine Version zurueck.

        Schritt 1 zeigt, welche Version je Datei kommen wuerde (und was warum
        uebersprungen wird), Schritt 2 fuehrt aus. Nur POST.
        """
        if request.method != "POST":
            raise SuspiciousOperation("Sammelaktion nur per POST.")
        if not self._can_restore(request):
            raise PermissionDenied

        aliases = versioned_aliases()
        alias = self._alias(request, aliases, source=request.POST)
        prefix = request.POST.get("prefix", "").strip()
        action = request.POST.get("action", "").strip()
        names = [n.strip() for n in request.POST.getlist("names") if n.strip()]

        if action != "restore_previous":
            raise SuspiciousOperation(f"Unbekannte Aktion '{action}'.")
        if not alias:
            raise SuspiciousOperation("Kein Storage angegeben.")
        if len(names) > BULK_LIMIT:
            raise SuspiciousOperation(f"Hoechstens {BULK_LIMIT} Dateien auf einmal.")

        back_url = self._changelist_url(alias, prefix, request.POST.get("marker"),
                                        request.POST.get("show") or "restorable")
        if not names:
            self.message_user(request, "Keine Datei ausgewaehlt.", messages.WARNING)
            return HttpResponseRedirect(back_url)

        storage = _get_storage(alias)
        planned, skipped = [], []
        for name in sorted(set(names)):
            history = storage.versions(name)
            if not history:
                skipped.append((name, "nicht im Bucket gefunden"))
                continue
            current = next((v for v in history if v.is_latest), history[0])
            real = [v for v in history if not v.is_delete_marker]
            if not real:
                skipped.append((name, "nur Delete-Marker, keine Daten zum Zurueckholen"))
                continue
            if len(real) == 1 and not current.is_delete_marker:
                skipped.append((name, "es gibt nur eine Version"))
                continue
            try:
                target = storage.pick(history, steps=1)
            except RestoreError as exc:
                skipped.append((name, str(exc)))
                continue
            if target.is_delete_marker:
                skipped.append((name, "davor lag nur ein Delete-Marker"))
            elif target.is_latest:
                skipped.append((name, "es gibt nur eine Version"))
            else:
                planned.append((name, target))

        # Schritt 1: Bestaetigungsseite
        if request.POST.get("confirm") != "yes":
            context = {
                **self.admin_site.each_context(request),
                "title": "Vorherige Version wiederherstellen?",
                "opts": self.model._meta,
                "alias": alias,
                "bucket": storage.bucket_name,
                "prefix": prefix,
                "marker": request.POST.get("marker", ""),
                "planned": planned,
                "skipped": skipped,
                "names": [n for n, _ in planned],
                "bulk_url": reverse("admin:s3restore_s3version_bulk"),
                "back_url": back_url,
            }
            return render(request, "admin/s3restore/s3version/bulk_confirmation.html",
                          context)

        # Schritt 2: ausfuehren
        done, failed = 0, 0
        for name, target in planned:
            result = storage.restore(name, version_id=target.version_id)
            if result.action == "restored":
                done += 1
                self._log(request, alias, name, target)
            elif result.action == "failed":
                failed += 1
                self.message_user(request, f"{name}: {result.reason}", messages.ERROR)
            else:
                self.message_user(request, f"{name}: {result.reason}", messages.WARNING)

        if done:
            self.message_user(
                request,
                f"{done} Datei(en) auf die vorherige Version zurueckgesetzt.",
                messages.SUCCESS)
        for name, reason in skipped:
            self.message_user(request, f"{name}: {reason}", messages.WARNING)
        if not done and not failed and not skipped:
            self.message_user(request, "Nichts geaendert.", messages.WARNING)

        return HttpResponseRedirect(back_url)

    def path_view(self, request):
        """Ganzen Pfad wiederherstellen, z.B. netzwerk/photos/.

        Zwei Modi: jede Datei eine Version zurueck, oder Stand zu einem
        Zeitpunkt. Dateien, die aktuell geloescht sind, kommen dabei zurueck.
        Der Pfad wird batchweise durchlaufen, nicht am Stueck geladen.
        """
        if request.method != "POST":
            raise SuspiciousOperation("Pfad-Rollback nur per POST.")
        if not self._can_restore(request):
            raise PermissionDenied

        aliases = versioned_aliases()
        alias = self._alias(request, aliases, source=request.POST)
        prefix = request.POST.get("prefix", "").strip()
        mode = request.POST.get("mode", "steps").strip()
        at_raw = request.POST.get("at", "").strip()

        if not alias:
            raise SuspiciousOperation("Kein Storage angegeben.")
        if mode not in ("steps", "at"):
            raise SuspiciousOperation(f"Unbekannter Modus '{mode}'.")
        if not prefix:
            # Ohne Praefix waere das ein Rollback des ganzen Buckets -- das
            # passiert hier nicht aus Versehen.
            self.message_user(request, "Bitte erst einen Praefix angeben.",
                              messages.WARNING)
            return HttpResponseRedirect(self._changelist_url(alias, ""))

        at = None
        if mode == "at":
            at = self._parse_at(at_raw)
            if at is None:
                self.message_user(
                    request,
                    f"'{at_raw}' ist kein gueltiger Zeitpunkt (z.B. 2026-08-18 18:00).",
                    messages.ERROR)
                return HttpResponseRedirect(self._changelist_url(alias, prefix))

        storage = _get_storage(alias)
        try:
            planned, skipped = self._plan_path(storage, prefix, at)
        except RestoreError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return HttpResponseRedirect(self._changelist_url(alias, prefix))

        # Schritt 1: Bestaetigungsseite
        if request.POST.get("confirm") != "yes":
            reasons = Counter(reason for _, reason in skipped)
            context = {
                **self.admin_site.each_context(request),
                "title": "Ganzen Pfad wiederherstellen?",
                "opts": self.model._meta,
                "alias": alias,
                "bucket": storage.bucket_name,
                "prefix": prefix,
                "mode": mode,
                "at": at,
                "at_raw": at_raw,
                "planned_count": len(planned),
                "preview": planned[:PATH_PREVIEW],
                "more": max(len(planned) - PATH_PREVIEW, 0),
                "skipped_reasons": sorted(reasons.items(), key=lambda r: -r[1]),
                "skipped_count": len(skipped),
                "path_url": reverse("admin:s3restore_s3version_path"),
                "back_url": self._changelist_url(alias, prefix),
            }
            return render(request, "admin/s3restore/s3version/path_confirmation.html",
                          context)

        # Schritt 2: ausfuehren
        done, failed = 0, 0
        for name, target in planned:
            result = storage.restore(name, version_id=target.version_id)
            if result.action == "restored":
                done += 1
                self._log(request, alias, name, target)
            elif result.action == "failed":
                failed += 1
                if failed <= 10:      # nicht hunderte Meldungen stapeln
                    self.message_user(request, f"{name}: {result.reason}", messages.ERROR)

        stand = (f"den Stand vom {timezone.localtime(at):%d.%m.%Y %H:%M}"
                 if at else "die jeweils vorherige Version")
        if done:
            self.message_user(
                request,
                f"{prefix}: {done} Datei(en) auf {stand} zurueckgesetzt"
                + (f", {len(skipped)} uebersprungen" if skipped else "") + ".",
                messages.SUCCESS)
        else:
            self.message_user(request, f"{prefix}: nichts geaendert.", messages.WARNING)
        if failed > 10:
            self.message_user(request, f"... und {failed - 10} weitere Fehler.",
                              messages.ERROR)

        return HttpResponseRedirect(self._changelist_url(alias, prefix))

    @staticmethod
    def _plan_path(storage, prefix, at):
        """Was wuerde im Pfad passieren? -> (planned, skipped)"""
        planned, skipped = [], []
        for name, history in storage.iter_under(prefix):
            current = next((v for v in history if v.is_latest), history[0])
            real = [v for v in history if not v.is_delete_marker]
            if not real:
                skipped.append((name, "nur Delete-Marker, keine Daten zum Zurueckholen"))
                continue
            if at is None and len(real) == 1 and not current.is_delete_marker:
                skipped.append((name, "es gibt nur eine Version"))
                continue
            try:
                target = storage.pick(history, steps=None if at else 1, at=at)
            except RestoreError:
                skipped.append((name, "existierte zu diesem Zeitpunkt noch nicht"))
                continue
            if target.is_delete_marker:
                skipped.append((name, "war zu diesem Zeitpunkt geloescht"))
            elif target.is_latest:
                skipped.append((name, "steht schon auf diesem Stand"))
            else:
                planned.append((name, target))
        return planned, skipped

    @staticmethod
    def _parse_at(value: str):
        """'2026-08-18T18:00' oder '2026-08-18 18:00' -> aware datetime."""
        if not value:
            return None
        parsed = parse_datetime(value.replace(" ", "T"))
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    # ------------------------------------------------------------------ Helfer
    @staticmethod
    def _alias(request, aliases: list[str], source=None) -> str | None:
        """Alias aus der Anfrage, aber nur wenn er wirklich konfiguriert ist."""
        data = source if source is not None else request.GET
        wanted = data.get("storage", "").strip()
        if wanted:
            if wanted not in aliases:
                raise SuspiciousOperation(f"Unbekannter Storage-Alias '{wanted}'.")
            return wanted
        return aliases[0] if aliases else None

    @staticmethod
    def _presigned_url(storage, version, expires: int = 300) -> str | None:
        """Kurzlebiger Link, um eine alte Version anzusehen, ohne sie zu aktivieren."""
        if version.is_delete_marker:
            return None
        try:
            return storage.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": storage.bucket_name, "Key": version.key,
                        "VersionId": version.version_id},
                ExpiresIn=expires)
        except Exception:  # pragma: no cover - z.B. fehlende Rechte
            return None

    @staticmethod
    def _versions_url(alias: str, name: str) -> str:
        return (reverse("admin:s3restore_s3version_versions")
                + "?" + urlencode({"storage": alias, "name": name}))

    @staticmethod
    def _show_mode(request) -> str:
        """Welcher Filter ist aktiv: restorable (Standard), deleted oder all."""
        wanted = request.GET.get("show", "").strip()
        if wanted in ("restorable", "live", "deleted", "all"):
            return wanted
        if request.GET.get("all") == "1":       # alte Links bleiben gueltig
            return "all"
        return "restorable"

    def _page_rows(self, storage, alias, prefix, marker, show):
        """Eine Seite ab dem Marker, gefiltert nach dem gewaehlten Modus.

        Gezaehlt wird, was der Filter wegnimmt: Dateien ohne aeltere Version
        (dort gibt es nichts zurueckzuholen) und -- im Modus "live" --
        geloeschte Dateien.
        """
        grouped, next_marker = storage.page_under(
            prefix, key_marker=marker or None, limit=PAGE_SIZE)
        rows, hidden, hidden_deleted = [], 0, 0
        for name, history in grouped.items():
            row = self._row(storage, alias, name, history)
            if not row["restorable"] and show != "all":
                hidden += 1
                continue
            if row["deleted"] and show == "live":
                hidden_deleted += 1
                continue
            rows.append(row)
        return rows, next_marker, hidden, hidden_deleted

    def _scan_deleted(self, storage, alias, prefix, marker):
        """Sucht vorwaerts nach geloeschten Dateien, bis die Seite voll ist.

        Geloeschtes ist meist duenn gesaet -- ein reines Filtern der aktuellen
        Seite wuerde fast leere Seiten liefern. Nach SCAN_LIMIT durchsuchten
        Objekten wird abgebrochen, damit ein Request nicht ewig laeuft; die
        Seite bietet dann einfach "Weiter" ab der letzten Stelle an.
        """
        rows, next_marker, scanned, capped = [], None, 0, False
        for name, history in storage.iter_under(prefix, key_marker=marker or None,
                                                batch=PAGE_SIZE * 2):
            scanned += 1
            row = self._row(storage, alias, name, history)
            if row["deleted"] and row["restorable"]:
                rows.append(row)
            if len(rows) >= PAGE_SIZE or scanned >= SCAN_LIMIT:
                next_marker = storage.key_for(name)
                capped = len(rows) < PAGE_SIZE
                break
        return rows, next_marker, scanned, capped

    def _row(self, storage, alias, name, history) -> dict:
        current = next((v for v in history if v.is_latest), history[0])
        return {
            "name": name,
            "count": len(history),
            "current": current,
            "deleted": current.is_delete_marker,
            "restorable": self._restorable(history, current),
            "url": self._versions_url(alias, name),
        }

    @staticmethod
    def _restorable(history, current) -> bool:
        """Gibt es hier ueberhaupt etwas zurueckzuholen?

        Nein bei genau einer Version (der aktuellen) und bei reinen
        Delete-Marker-Leichen. Ja, wenn es eine aeltere Version gibt -- oder
        wenn die Datei aktuell geloescht ist und es eine echte Version gibt.
        """
        real = [v for v in history if not v.is_delete_marker]
        if not real:
            return False
        return len(real) > 1 or current.is_delete_marker

    @staticmethod
    def _changelist_url(alias: str, prefix: str = "", marker: str | None = None,
                        show: str = "restorable") -> str:
        params = {"storage": alias, "prefix": prefix}
        if marker:
            params["marker"] = marker
        if show and show != "restorable":
            params["show"] = show
        return (reverse("admin:s3restore_s3version_changelist") + "?" + urlencode(params))

    @staticmethod
    def _track_page(request, alias: str, prefix: str, marker: str) -> tuple[int, str | None]:
        """Merkt sich die Startmarker der besuchten Seiten in der Session.

        S3 blaettert nur vorwaerts -- fuer "Zurueck" und die Seitennummer
        braucht es diese Kette. Sie haengt an (Storage, Praefix) und wird beim
        Sprung auf die erste Seite zurueckgesetzt.
        """
        stacks = request.session.get(SESSION_KEY, {})
        bucket_key = f"{alias}\n{prefix}"
        stack = stacks.get(bucket_key, [])

        if not marker:
            stack = [""]
        elif marker in stack:                      # Rueckwaerts- oder Direktsprung
            stack = stack[:stack.index(marker) + 1]
        else:
            stack = (stack or [""]) + [marker]

        stacks[bucket_key] = stack[-MARKER_STACK_LIMIT:]
        request.session[SESSION_KEY] = stacks
        request.session.modified = True

        prev_marker = stack[-2] if len(stack) > 1 else None
        return len(stack), prev_marker

    def _log(self, request, alias, name, target):
        """Wiederherstellung in der Admin-Historie festhalten."""
        try:
            # Direkt angelegt statt ueber log_action()/log_actions(): das Objekt
            # existiert nur in S3, hat also keine echte pk fuer den Manager.
            LogEntry(
                user_id=request.user.pk,
                content_type_id=get_content_type_for_model(self.model).pk,
                object_id=name,
                object_repr=f"{alias}:{name}"[:200],
                action_flag=CHANGE,
                change_message=f"Wiederhergestellt auf Version {target.version_id} "
                               f"({target.last_modified:%Y-%m-%d %H:%M:%S} UTC)",
            ).save()
        except Exception:  # pragma: no cover - Logging darf nie den Rollback kippen
            pass
