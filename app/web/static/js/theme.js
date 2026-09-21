// Theme toggle: auto (follow the OS) -> light -> dark -> auto.
// The choice is read back in an inline script in <head>, before first paint, so switching
// pages never flashes the other theme.
(function () {
  var KEY = "newsdesk-theme";
  var ORDER = ["auto", "light", "dark"];
  var button = document.querySelector("[data-theme-toggle]");
  if (!button) return;

  function current() {
    try {
      var saved = localStorage.getItem(KEY);
      return ORDER.indexOf(saved) === -1 ? "auto" : saved;
    } catch (e) {
      return "auto"; // private mode, or storage blocked
    }
  }

  function apply(choice) {
    if (choice === "auto") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", choice);
    }
    button.textContent = "Theme: " + choice;
    button.setAttribute("aria-label", "Colour theme: " + choice + ". Click to change.");
  }

  apply(current());

  button.addEventListener("click", function () {
    var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {
      // Not being able to remember it is fine; the click still takes effect.
    }
    apply(next);
  });
})();
