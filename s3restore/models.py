"""
s3restore/models.py

S3Version ist ein reines Platzhalter-Modell: es hat keine Tabelle
(`managed = False`) und wird nie abgefragt. Es existiert nur, damit die
Wiederherstellungs-Seite im Django-Admin an der gewohnten Stelle auftaucht
(Index-Eintrag, Breadcrumbs, URL-Namensraum, Rechteverwaltung).

Die eigentlichen Daten kommen live aus S3 -- siehe s3restore/admin.py.
"""

from django.db import models


class S3Version(models.Model):
    id = models.AutoField(primary_key=True)  # nie benutzt, verhindert nur W042

    class Meta:
        managed = False                      # keine Tabelle, keine Migration noetig
        default_permissions = ()             # kein add/change/delete/view
        permissions = [
            ("view_s3version", "Darf S3-Versionen ansehen"),
            ("restore_s3version", "Darf S3-Versionen wiederherstellen"),
        ]
        verbose_name = "S3-Objektversion"
        verbose_name_plural = "S3-Wiederherstellung"

    def __str__(self) -> str:  # pragma: no cover
        return "S3-Objektversion"
