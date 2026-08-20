"""ROOT_URLCONF fuer die Tests -- im echten Projekt reicht der uebliche
    path("admin/", admin.site.urls)
in urls.py, die Admin-Seite haengt sich selbst ein."""
from django.contrib import admin
from django.urls import path

urlpatterns = [path("admin/", admin.site.urls)]
