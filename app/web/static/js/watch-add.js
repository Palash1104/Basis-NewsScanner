// The two dialogs on /watchlist: "Add a stock" (user's design, 2026-10-17) and a stock's own
// dialog, opened from its card (user, 2026-10-18). Both close the same ways - Close, Escape, a
// click outside - lock the page's scroll while open, and switch tabs the same way.
//
// "Add a stock": its button opens it. Without this script the button's #watch-add still opens
// it through CSS :target, and Close (href="#") still closes it; with it, `open` and `closed`
// decide, so closing works however it was opened (a failed add comes back with it open, by
// class, not by :target).
//
// A stock's dialog: the card's stretched button (`data-scan-open`, shipped hidden and revealed
// here, so a page without JavaScript has no dead control) opens it and sends the hidden form,
// which asks the scanner for a scan (POST /watchlist/scan). Its body polls while the scan is on
// its way and stops by itself when it is done. Closing empties the body - so no poll outlives
// the dialog - and redraws that card and the news list, so what the scan found is on the page.
//
// The page around them (user's design, 2026-10-19): a card's "⋮" menu (a <details>) closes on a
// click elsewhere, on Escape and when its item opens the dialog; the banner's × removes the
// banner and takes added=/removed=/error= off the address, so a reload doesn't bring it back.
(function () {
  "use strict";

  var ADD = "watch-add";
  var STOCK = "watch-stock";

  function byId(id) {
    return document.getElementById(id);
  }

  function isOpen(box) {
    return box.classList.contains("open") || (box.matches(":target") && !box.classList.contains("closed"));
  }

  function openBox(box, focus) {
    box.classList.remove("closed");
    box.classList.add("open");
    document.documentElement.classList.add("watch-add-locked");
    if (focus) {
      focus.focus();
    }
  }

  function closeBox(box) {
    box.classList.remove("open");
    box.classList.add("closed");
    if (!document.querySelector(".watch-add.open")) {
      document.documentElement.classList.remove("watch-add-locked");
    }
    if (location.hash === "#" + box.id) {
      history.replaceState(null, "", location.pathname + location.search);
    }
    if (box.id === STOCK) {
      closeStock(box);
    } else {
      var opener = document.querySelector('a[href="#watch-add"]');
      if (opener) {
        opener.focus();
      }
    }
  }

  // ------------------------------------------------------------ a stock's dialog

  var openedFrom = null; // the card's button, for focus and the redraw on closing

  function openStock(button) {
    var box = byId(STOCK);
    var form = byId("watch-scan-form");
    var opening = byId("watch-scan-opening");
    var body = byId("watch-scan-body");
    if (!box || !form || !opening || !body) {
      return;
    }
    openedFrom = button;
    var name = button.dataset.scanName || button.dataset.scanOpen;
    var title = box.querySelector("[data-stock-title]");
    if (title) {
      title.textContent = name;
    }
    // The opening wait, until the scanner's first answer replaces it.
    var fresh = opening.content.firstElementChild.cloneNode(true);
    var waitName = fresh.querySelector("[data-wait-name]");
    if (waitName) {
      waitName.textContent = name;
    }
    body.replaceWith(fresh);
    form.elements.symbol.value = button.dataset.scanOpen;
    openBox(box, box.querySelector("[data-watch-close]"));
    htmx.trigger(form, "submit");
  }

  function closeStock() {
    // An empty body: whatever was polling is gone from the page, so htmx stops asking.
    var body = byId("watch-scan-body");
    if (body) {
      var empty = document.createElement("div");
      empty.id = "watch-scan-body";
      empty.className = "watch-pv watch-scan";
      body.replaceWith(empty);
    }
    var button = openedFrom;
    openedFrom = null;
    if (!button) {
      return;
    }
    var symbol = button.dataset.scanOpen;
    var card = document.querySelector('[data-card-symbol="' + CSS.escape(symbol) + '"]');
    if (card) {
      htmx
        .ajax("GET", "/watchlist/card?symbol=" + encodeURIComponent(symbol), { target: card, swap: "outerHTML" })
        .then(function () {
          var redrawn = document.querySelector('[data-card-symbol="' + CSS.escape(symbol) + '"] [data-scan-open]');
          if (redrawn) {
            redrawn.hidden = false;
            redrawn.focus();
          }
        });
    } else {
      button.focus();
    }
    var news = byId("watch-news-list");
    if (news) {
      htmx.ajax("GET", "/watchlist/news", { target: news, swap: "outerHTML" });
    }
  }

  function revealCardButtons(root) {
    (root || document).querySelectorAll("[data-scan-open][hidden]").forEach(function (button) {
      button.hidden = false;
    });
  }

  // ------------------------------------------------------------ the cards' menus, the banner

  function closeMenus(except) {
    document.querySelectorAll(".watch-card-menu[open]").forEach(function (menu) {
      if (menu !== except) {
        menu.open = false;
      }
    });
  }

  function dismissFlash(link) {
    var banner = link.closest("[data-flash]");
    if (banner) {
      banner.remove();
    }
    var params = new URLSearchParams(location.search);
    ["added", "removed", "error"].forEach(function (key) {
      params.delete(key);
    });
    var query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
  }

  // ------------------------------------------------------------ clicks and keys

  document.addEventListener("click", function (event) {
    var target = event.target;
    closeMenus(target.closest(".watch-card-menu"));
    var cardButton = target.closest("[data-scan-open]");
    if (cardButton) {
      event.preventDefault();
      closeMenus(null);
      openStock(cardButton);
      return;
    }
    var flashClose = target.closest("[data-flash-close]");
    if (flashClose) {
      event.preventDefault();
      dismissFlash(flashClose);
      return;
    }
    var picked = target.closest(".watch-suggest");
    if (picked) {
      // The row whose company the preview shows stays marked, and the wait names it.
      document.querySelectorAll(".watch-suggest.is-picked").forEach(function (row) {
        row.classList.remove("is-picked");
      });
      picked.classList.add("is-picked");
      var name = document.querySelector("#watch-add [data-wait-name]");
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
    var add = byId(ADD);
    if (add && target.closest('a[href="#watch-add"]')) {
      event.preventDefault();
      openBox(add, byId("watch-search"));
      return;
    }
    var closer = target.closest("[data-watch-close]");
    if (closer) {
      event.preventDefault();
      closeBox(closer.closest(".watch-add"));
      return;
    }
    if (target.classList && target.classList.contains("watch-add") && isOpen(target)) {
      closeBox(target); // the dimmed page around the dialog
    }
  });

  // The tabs: one panel shown, its tab marked.
  function showTab(container, name) {
    if (!container || !name) {
      return;
    }
    container.querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
    });
    container.querySelectorAll("[data-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.panel !== name;
    });
    var body = container.closest(".watch-add-body");
    if (body) {
      body.scrollTop = 0;
    }
  }

  // A source filter: the headlines in that panel from one channel, or all.
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
    if (event.key !== "Escape") {
      return;
    }
    var menu = document.querySelector(".watch-card-menu[open]");
    if (menu) {
      menu.open = false;
      menu.querySelector("summary").focus();
      return;
    }
    [byId(STOCK), byId(ADD)].forEach(function (box) {
      if (box && isOpen(box)) {
        closeBox(box);
      }
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    revealCardButtons(document);
    var add = byId(ADD);
    if (add && isOpen(add)) {
      openBox(add, byId("watch-search")); // opened by the server (a failed add) or by #watch-add
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

  // A card redrawn after its dialog closed carries its button hidden again.
  document.addEventListener("htmx:afterSettle", function (event) {
    revealCardButtons(event.target && event.target.parentNode ? event.target.parentNode : document);
  });
})();
