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

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.options import get_content_type_for_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
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


BROWSE_LIMIT = 5000  # so viele Objekte holt die Uebersicht hoechstens aus S3
PAGE_SIZE = 50       # Objekte pro Seite
BULK_LIMIT = 200     # so viele Dateien nimmt die Sammelaktion hoechstens entgegen


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

        rows, truncated, page_obj, page_links = [], False, None, []
        if alias:
            storage = _get_storage(alias)
            try:
                grouped = storage.versions_under(prefix)
            except RestoreError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                grouped = {}

            names = sorted(grouped)
            truncated = len(names) > BROWSE_LIMIT
            paginator = Paginator(names[:BROWSE_LIMIT], PAGE_SIZE)
            try:
                page_obj = paginator.page(request.GET.get("page") or 1)
            except PageNotAnInteger:
                page_obj = paginator.page(1)
            except EmptyPage:
                page_obj = paginator.page(paginator.num_pages)
            page_links = self._page_links(paginator, page_obj, alias, prefix)

            for name in page_obj.object_list:
                history = grouped[name]
                current = next((v for v in history if v.is_latest), history[0])
                rows.append({
                    "name": name,
                    "count": len(history),
                    "current": current,
                    "deleted": current.is_delete_marker,
                    "url": self._versions_url(alias, name),
                })

        context = {
            **self.admin_site.each_context(request),
            "title": "S3-Wiederherstellung",
            "opts": self.model._meta,
            "aliases": aliases,
            "alias": alias,
            "bucket": getattr(_get_storage(alias), "bucket_name", "") if alias else "",
            "prefix": prefix,
            "rows": rows,
            "truncated": truncated,
            "limit": BROWSE_LIMIT,
            "page_obj": page_obj,
            "page_links": page_links,
            "page": page_obj.number if page_obj else 1,
            "total": page_obj.paginator.count if page_obj else 0,
            "can_restore": self._can_restore(request),
            "bulk_url": reverse("admin:s3restore_s3version_bulk"),
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

        back_url = self._changelist_url(alias, prefix, request.POST.get("page"))
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
                "page": request.POST.get("page", ""),
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
    def _changelist_url(alias: str, prefix: str = "", page: int | str | None = None) -> str:
        params = {"storage": alias, "prefix": prefix}
        if page and str(page) != "1":
            params["page"] = page
        return (reverse("admin:s3restore_s3version_changelist") + "?" + urlencode(params))

    @classmethod
    def _page_links(cls, paginator, page_obj, alias, prefix) -> list[dict]:
        """Seitenzahlen fuer die Blaetter-Leiste; None = Auslassung (…)."""
        if paginator.num_pages <= 1:
            return []
        links = []
        for number in paginator.get_elided_page_range(page_obj.number,
                                                      on_each_side=2, on_ends=1):
            if number == paginator.ELLIPSIS:
                links.append({"number": None})
            else:
                links.append({
                    "number": number,
                    "current": number == page_obj.number,
                    "url": cls._changelist_url(alias, prefix, number),
                })
        return links

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
