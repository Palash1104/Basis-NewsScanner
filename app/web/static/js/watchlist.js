// The rail's watchlist, and the dialog that edits it.
//
// The page arrives with the settings.yaml default already rendered, so it reads correctly
// with no JavaScript at all. If this browser has its own list saved, the rows are re-fetched
// from /watchlist as a fragment - the server still decides what a symbol means and drops any
// it doesn't know, so nothing a browser has stored can put a made-up asset on the page.
//
// The choice lives in localStorage because the web app never writes: it opens the database
// read-only, and a watchlist is one person's on one machine, not a fact about the news.
(function () {
  var KEY = "newsdesk-watchlist";
  var editor = document.getElementById("watchlist-editor");
  var rows = document.getElementById("watchlist-rows");
  if (!editor || !rows) return;
  var max = Number(editor.getAttribute("data-max")) || 12;

  function saved() {
    try {
      var raw = localStorage.getItem(KEY);
      var list = raw ? JSON.parse(raw) : null;
      return Array.isArray(list) ? list : null;
    } catch (e) {
      return null; // private mode, blocked storage, or something else's key
    }
  }

  function remember(symbols) {
    try {
      localStorage.setItem(KEY, JSON.stringify(symbols));
    } catch (e) {
      // Not being able to remember it is fine; the list still changes on screen.
    }
  }

  function forget() {
    try {
      localStorage.removeItem(KEY);
    } catch (e) {}
  }

  function onScreen() {
    return (rows.getAttribute("data-symbols") || "").split(",").filter(Boolean);
  }

  function show(symbols) {
    var query = symbols.length ? "?symbols=" + encodeURIComponent(symbols.join(",")) : "";
    return fetch("/watchlist" + query, { headers: { "HX-Request": "true" } })
      .then(function (response) {
        return response.ok ? response.text() : null;
      })
      .then(function (html) {
        if (!html) return; // leave what is on screen rather than blanking the rail
        rows.outerHTML = html;
        rows = document.getElementById("watchlist-rows");
      })
      .catch(function () {});
  }

  function boxes() {
    return editor.querySelectorAll('input[name="symbol"]');
  }

  function chosen() {
    var picked = [];
    boxes().forEach(function (box) {
      if (box.checked) picked.push(box.value);
    });
    return picked;
  }

  // At the cap, the unchecked boxes stop answering: the limit is visible before it bites.
  function applyCap() {
    var full = chosen().length >= max;
    boxes().forEach(function (box) {
      box.disabled = full && !box.checked;
    });
  }

  function open() {
    var showing = onScreen();
    boxes().forEach(function (box) {
      box.checked = showing.indexOf(box.value) !== -1;
    });
    applyCap();
    editor.showModal();
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    if (event.target.closest("[data-watchlist-edit]")) open();
    if (event.target.closest("[data-watchlist-reset]")) {
      forget();
      show([]).then(function () {
        editor.close("reset");
      });
    }
  });

  editor.addEventListener("change", applyCap);

  editor.addEventListener("close", function () {
    if (editor.returnValue !== "save") return;
    var picked = chosen();
    remember(picked);
    show(picked);
  });

  var mine = saved();
  if (mine && mine.join(",") !== onScreen().join(",")) show(mine);
})();
