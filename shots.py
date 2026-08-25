"""Screenshots der Admin-Seiten (nicht Teil der Testsuite): pytest shots.py"""
import time
from urllib.parse import urlencode

import boto3
import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from moto import mock_aws
from playwright.sync_api import sync_playwright

BUCKET = "garten-backup"
NAME = "config/zones.json"
IMG = "docs/img"


@pytest.mark.django_db(transaction=True)
def test_make_screenshots(live_server):
    with mock_aws():
        c = boto3.client("s3", region_name="eu-central-1")
        c.create_bucket(Bucket=BUCKET,
                        CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        c.put_bucket_versioning(Bucket=BUCKET,
                                VersioningConfiguration={"Status": "Enabled"})
        from django.core.files.storage import storages
        storages._storages.clear()
        st = storages["default"]

        for name, texts in {
            NAME: ['{"zone": 1}', '{"zone": 1, "dauer": 300}', '{"zone": 1, "dauer": 420}'],
            "config/kalibrierung.json": ["{}", '{"gyro_offset": 0.4}'],
            "config/zeitplan.yaml": ["mo: 06:00", "mo: 06:30", "mo: 05:45"],
        }.items():
            for t in texts:
                time.sleep(1.05)
                st.save(name, ContentFile(t.encode()))

        User.objects.create_superuser("chef", "chef@example.com", "geheim123")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 760})
            page.goto(f"{live_server.url}/admin/login/")
            page.fill("#id_username", "chef")
            page.fill("#id_password", "geheim123")
            page.click("input[type=submit]")
            page.wait_for_load_state("networkidle")

            # 1: Uebersicht mit Checkboxen, zwei Dateien ausgewaehlt
            page.goto(f"{live_server.url}/admin/s3restore/s3version/?prefix=config/")
            page.wait_for_load_state("networkidle")
            boxes = page.query_selector_all("input.action-select")
            boxes[0].check(); boxes[2].check()
            page.screenshot(path=f"{IMG}/uebersicht.png", full_page=True)

            # 2: Sammel-Bestaetigung
            page.click("#run-action")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/sammelaktion.png", full_page=True)
            page.click("input[value^='Ja,']")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/sammelaktion-erfolg.png", full_page=True)

            # 3: Versionsliste eines Objekts
            page.goto(f"{live_server.url}/admin/s3restore/s3version/versions/?"
                      + urlencode({"storage": "default", "name": NAME}))
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/versionen.png", full_page=True)

            # 4: Einzel-Bestaetigung und Ergebnis
            page.click("input[value='Ausgewaehlte Version wiederherstellen']")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/bestaetigung.png", full_page=True)
            page.click("input[value='Ja, wiederherstellen']")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/erfolg.png", full_page=True)
            assert "zurueckgesetzt" in page.content()

            # 5: Pfad-Rollback -- Formular und Bestaetigungsseite
            for name in ("netzwerk/photos/strand.jpg", "netzwerk/photos/2024/beet.jpg",
                         "netzwerk/photos/zaun.jpg"):
                for text in ("alt", "neu"):
                    time.sleep(1.05)
                    st.save(name, ContentFile(text.encode()))
            page.goto(f"{live_server.url}/admin/s3restore/s3version/?prefix=netzwerk/photos/")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/pfad-formular.png", full_page=True)
            page.click("input[value='Pfad wiederherstellen']")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/pfad-bestaetigung.png", full_page=True)

            # 6: Filter "nur geloeschte"
            time.sleep(1.05)
            st.delete("netzwerk/photos/zaun.jpg")
            page.goto(f"{live_server.url}/admin/s3restore/s3version/"
                      "?prefix=netzwerk/&show=deleted")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/filter-geloescht.png", full_page=True)
            page.goto(f"{live_server.url}/admin/s3restore/s3version/"
                      "?prefix=netzwerk/&show=live")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{IMG}/filter-ohne-geloeschte.png", full_page=True)

            # 7: Blaettern -- 120 Dateien unter einem zweiten Praefix
            for i in range(120):
                for body in (b"alt", b"x"):
                    st.s3_client.put_object(Bucket=BUCKET,
                                            Key=st.key_for(f"logs/lauf-{i:03d}.txt"),
                                            Body=body)
            page.goto(f"{live_server.url}/admin/s3restore/s3version/?prefix=logs/")
            page.wait_for_load_state("networkidle")
            page.click("text=Weiter")
            page.wait_for_load_state("networkidle")
            page.locator("p.paginator").scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.screenshot(path=f"{IMG}/pagination.png")

            browser.close()
