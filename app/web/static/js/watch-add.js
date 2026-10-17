// "Add a stock" on /watchlist: a dialog over the page (user's design, 2026-10-17).
// The "Add a stock" button opens it, and Close, Escape or a click outside it close it. Without
// this script the button's #watch-add still opens it through CSS :target, and Close (href="#")
// still closes it; with it, `open` and `closed` decide, so closing works however it was opened
// (a failed add comes back with it open, by class, not by :target).
(function () {
  "use strict";

  function dialog() {
    return document.getElementById("watch-add");
  }

  function isOpen(box) {
    return box.classList.contains("open") || (box.matches(":target") && !box.classList.contains("closed"));
  }

  function open(box) {
    box.classList.remove("closed");
    box.classList.add("open");
    document.documentElement.classList.add("watch-add-locked");
    var search = document.getElementById("watch-search");
    if (search) {
      search.focus();
    }
  }

  function close(box) {
    box.classList.remove("open");
    box.classList.add("closed");
    document.documentElement.classList.remove("watch-add-locked");
    if (location.hash === "#watch-add") {
      history.replaceState(null, "", location.pathname + location.search);
    }
    var opener = document.querySelector('a[href="#watch-add"]');
    if (opener) {
      opener.focus();
    }
  }

  document.addEventListener("click", function (event) {
    var box = dialog();
    if (!box) {
      return;
    }
    var target = event.target;
    var picked = target.closest(".watch-suggest");
    if (picked) {
      // The row whose company the preview shows stays marked, and the wait names it.
      document.querySelectorAll(".watch-suggest.is-picked").forEach(function (row) {
        row.classList.remove("is-picked");
      });
      picked.classList.add("is-picked");
      var name = document.querySelector("[data-wait-name]");
      if (name && picked.dataset.name) {
        name.textContent = picked.dataset.name;
      }
      return;
    }
    var tab = target.closest("[data-tab], [data-tab-go]");
    if (tab) {
      showTab(tab.closest(".watch-pv"), tab.dataset.tab || tab.dataset.tabGo);
      return;
    }
    if (target.closest('a[href="#watch-add"]')) {
      event.preventDefault();
      open(box);
    } else if (target.closest("[data-watch-close]")) {
      event.preventDefault();
      close(box);
    } else if (target === box && isOpen(box)) {
      close(box); // the dimmed page around the dialog
    }
  });

  // The preview's tabs: one panel shown, its tab marked.
  function showTab(form, name) {
    if (!form || !name) {
      return;
    }
    form.querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
    });
    form.querySelectorAll("[data-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.panel !== name;
    });
    var body = form.closest(".watch-add-body");
    if (body) {
      body.scrollTop = 0;
    }
  }

  // "All sources" / "Google News" / "Stored by BASIS": the headlines in that panel.
  document.addEventListener("change", function (event) {
    var select = event.target.closest("[data-headline-filter]");
    if (!select) {
      return;
    }
    var panel = select.closest("[data-panel]");
    panel.querySelectorAll(".watch-hl").forEach(function (row) {
      row.hidden = select.value !== "" && row.dataset.where !== select.value;
    });
  });

  // Arrow keys move between the tabs, as a tab list does.
  document.addEventListener("keydown", function (event) {
    var tab = event.target.closest && event.target.closest("[role=tab][data-tab]");
    if (!tab || (event.key !== "ArrowRight" && event.key !== "ArrowLeft")) {
      return;
    }
    var tabs = Array.prototype.slice.call(tab.parentNode.querySelectorAll("[data-tab]"));
    var next = tabs[(tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.focus();
    showTab(tab.closest(".watch-pv"), next.dataset.tab);
  });

  document.addEventListener("keydown", function (event) {
    var box = dialog();
    if (event.key === "Escape" && box && isOpen(box)) {
      close(box);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    var box = dialog();
    if (box && isOpen(box)) {
      open(box); // opened by the server (a failed add) or by the address's #watch-add
    }
  });

  // A new preview starts at its top, not where the last one was scrolled to.
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "watch-preview") {
      var body = event.target.closest(".watch-add-body");
      if (body) {
        body.scrollTop = 0;
      }
    }
  });
})();
