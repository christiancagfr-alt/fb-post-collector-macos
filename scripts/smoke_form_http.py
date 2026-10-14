"""Verify that the packaged server includes the native form 403 fix."""
import urllib.parse
import urllib.request


def verify_project_forms(base):
    with urllib.request.urlopen(base + "/", timeout=15) as response:
        assert response.headers.get("Referrer-Policy") == "same-origin"
    for mode in ("post_lookup", "post", "page"):
        body = urllib.parse.urlencode({"name": "Package form smoke", "project_type": mode}).encode()
        request = urllib.request.Request(base + "/projects/new", data=body, headers={
            "Origin": base, "Referer": base + "/", "Sec-Fetch-Site": "same-origin"})
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert 'id="project-form"' in html
            assert f'value="{mode}"' in html
    print("Bundled project form creation and redirects passed for all three modes")
