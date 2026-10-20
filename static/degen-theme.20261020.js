/* Run before first paint. Keep dairy preferences separate from the sports app. */
(function () {
  "use strict";
  var key = "dairy-theme";
  var root = document.documentElement;
  function normalize(value) {
    return value === "ivory" ? "ivory" : "charcoal";
  }
  function apply(value) {
    var theme = normalize(value);
    root.setAttribute("data-theme", theme);
    root.style.colorScheme = theme === "ivory" ? "light" : "dark";
    var select = document.getElementById("themeSelect");
    if (select) select.value = theme;
    return theme;
  }
  var saved;
  try { saved = localStorage.getItem(key); } catch (_) {}
  var initial = apply(saved);
  // Migrate the old green canvas to Degen's dark palette, retaining light users.
  try { localStorage.setItem(key, initial); } catch (_) {}
  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("themeSelect");
    if (!select) return;
    select.setAttribute("aria-label", "Colour theme");
    select.value = root.getAttribute("data-theme");
    select.addEventListener("change", function () {
      var theme = apply(select.value);
      try { localStorage.setItem(key, theme); } catch (_) {}
    });
  });
  window.addEventListener("storage", function (event) {
    if (event.key === key) apply(event.newValue);
  });
})();
