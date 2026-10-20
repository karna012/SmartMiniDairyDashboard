"""Read-only browser checks. Does not import the app or touch its database.

--preview substitutes the candidate theme in browser responses, before deployment.
The test never submits forms. Admin GETs exercise the existing cookie auth only.
"""
import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://degenpredictions.com"
PREFIX = "/smartdairy"
CSS = "degen-theme.20261020.css"
JS = "degen-theme.20261020.js"
LEGACY = re.compile(
    r"    <script>\s*"
    r"\(function\(\)\{\s*var key='dairy-theme'[\s\S]*?"
    r"\}\)\(\);\s*</script>"
)
COLORS = """() => {
  const root = getComputedStyle(document.documentElement);
  const body = getComputedStyle(document.body);
  const section = document.querySelector('section, .login-page .max-w-md');
  const control = document.querySelector('input:not([type=hidden]), select');
  return {
    theme: document.documentElement.dataset.theme,
    bg: body.backgroundColor, text: body.color,
    panel: section && getComputedStyle(section).backgroundColor,
    control: control && getComputedStyle(control).backgroundColor,
    accent: root.getPropertyValue('--accent').trim(),
    overflow: document.documentElement.scrollWidth > innerWidth + 1,
    themeOptions: Array.from(document.querySelectorAll('#themeSelect option'), x => x.value)
  };
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    def route_request(route):
        request = route.request
        if request.method not in ("GET", "HEAD"):
            route.abort()
            return
        if args.preview and request.url.endswith(("/" + CSS, "/" + JS)):
            filename = request.url.rsplit("/", 1)[-1]
            route.fulfill(
                path=str(ROOT / "static" / filename),
                content_type="text/css" if filename.endswith(".css") else "text/javascript",
            )
        elif args.preview and request.resource_type == "document" and PREFIX in request.url:
            response = route.fetch()
            html = response.text()
            html = re.sub(
                r'<link rel="stylesheet" href="/smartdairy/static/style\.v2\.css" />',
                f'<script src="{PREFIX}/static/{JS}"></script>'
                f'<link rel="stylesheet" href="{PREFIX}/static/{CSS}" />',
                html,
            )
            html = LEGACY.sub("", html)
            html = re.sub(r'<option value="emerald">Emerald</option>', "", html)
            route.fulfill(response=response, body=html)
        else:
            route.continue_()

    with sync_playwright() as pw:
        browser = (
            pw.chromium.launch(
                executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                headless=True,
            ) if args.browser == "chromium" else pw.webkit.launch(headless=True)
        )
        comparison = browser.new_page()
        comparison.goto(ORIGIN + "/", wait_until="domcontentloaded")
        comparison.wait_for_function(
            "getComputedStyle(document.documentElement).getPropertyValue('--dp-canvas').trim() !== ''"
        )
        reference = comparison.evaluate("""() => {
          const s = getComputedStyle(document.documentElement);
          return Object.fromEntries(['canvas','surface','accent','text'].map(
            key => [key, s.getPropertyValue('--dp-' + key).trim()]));
        }""")
        print("Degen reference:", json.dumps(reference), flush=True)
        comparison.close()
        assert reference == {
            "canvas": "#070a14", "surface": "#0e1424", "accent": "#10b981", "text": "#eef2fb",
        }, reference

        widths = [1440, 390, 320] if args.browser == "chromium" else [390]
        for width in widths:
            context = browser.new_context(viewport={"width": width, "height": 920})
            context.route("**/*", route_request)
            context.add_cookies([{
                "name": "admin_authenticated", "value": "true",
                "domain": "degenpredictions.com", "path": "/",
            }])
            # An existing user's old green preference must migrate as well.
            context.add_init_script(
                "try { if (!localStorage.getItem('dairy-theme')) "
                "localStorage.setItem('dairy-theme','emerald'); } catch (_) {}"
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            paths = ["/", "/login", "/admin", "/milk-yield", "/inventory-history"]
            for path in paths:
                response = page.goto(ORIGIN + PREFIX + path, wait_until="domcontentloaded")
                page.wait_for_function(
                    "document.documentElement.dataset.theme === 'charcoal'"
                )
                page.wait_for_timeout(350)
                assert response.status == 200, (path, response.status)
                assert page.locator(f'link[href$="/{CSS}"]').count() == 1, path
                colors = page.evaluate(COLORS)
                print(args.browser, width, "cow-detail" if "/cows/" in path else path,
                      json.dumps(colors), flush=True)
                if colors["bg"] != "rgb(7, 10, 20)":
                    print(page.evaluate("""() => ({
                      variables: ['--bg','--text','--panel-strong'].map(k => [
                        k, getComputedStyle(document.documentElement).getPropertyValue(k),
                        getComputedStyle(document.body).getPropertyValue(k),
                        getComputedStyle(document.querySelector('section')).getPropertyValue(k)]),
                      propertyRules: [...document.styleSheets].filter(s => !s.href)
                        .flatMap(s => [...s.cssRules]).map(r => r.cssText)
                        .filter(t => t.includes('@property') || t.includes('--bg:') || t.includes('--text:')),
                      rules: [...document.styleSheets].filter(s => s.href && s.href.includes('degen-theme'))
                        .flatMap(s => [...s.cssRules]).filter(r => r.selectorText === 'body')
                        .map(r => r.cssText)
                    })"""), flush=True)
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "debug-theme.png"))
                assert colors["bg"] == "rgb(7, 10, 20)", colors
                assert colors["text"] == "rgb(238, 242, 251)", colors
                assert colors["accent"] == reference["accent"], colors
                assert colors["panel"] == "rgb(14, 20, 36)", colors
                assert not colors["overflow"], (path, colors)
                assert colors["themeOptions"] == ["charcoal", "ivory"], colors
                if path == "/":
                    cow = page.locator(f'a[href^="{PREFIX}/cows/"]').first
                    if cow.count() and not any("/cows/" in p for p in paths):
                        paths.append(cow.get_attribute("href")[len(PREFIX):])
                if path == "/admin":
                    assert page.locator('form[action$="/admin/entry"]').count() > 0
                    danger = page.locator(".btn-danger").first
                    if danger.count():
                        assert danger.evaluate(
                            "e => getComputedStyle(e).color"
                        ) == "rgb(255, 117, 151)"
                if args.screenshots and path in ("/", "/login", "/admin"):
                    label = path.strip("/") or "dashboard"
                    page.screenshot(path=str(args.screenshots / f"{args.browser}-{width}-{label}.png"))
                assert not errors, errors
            # The existing optional light choice still works across navigation.
            page.select_option("#themeSelect", "ivory")
            page.reload(wait_until="domcontentloaded")
            assert page.evaluate(COLORS)["bg"] == "rgb(244, 246, 252)"
            page.select_option("#themeSelect", "charcoal")
            page.reload(wait_until="domcontentloaded")
            assert page.evaluate(COLORS)["bg"] == "rgb(7, 10, 20)"
            assert not errors, errors
            context.close()

        # Restricted/private-mode storage must not break initialization.
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.route("**/*", route_request)
        context.add_init_script("""
          Object.defineProperty(window, 'localStorage', {
            get() { throw new DOMException('Storage blocked', 'SecurityError'); }
          });
        """)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(ORIGIN + PREFIX + "/login", wait_until="domcontentloaded")
        page.wait_for_function("document.documentElement.dataset.theme === 'charcoal'")
        page.select_option("#themeSelect", "ivory")
        assert not errors, errors
        context.close()
        browser.close()
        print("PASS: palette, pages, mobile overflow, theme persistence, restricted storage.", flush=True)


if __name__ == "__main__":
    main()
