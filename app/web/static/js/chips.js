// The "assets affected" strip: one scrolling row per story, with a button at each end.
//
// The buttons live in the template but start hidden, so a page without JavaScript shows no
// control that does nothing; this file reveals them only for a row that actually overflows,
// and disables the one pointing at an end already reached. Clicks are handled on the document
// rather than bound per row, because HTMX swaps the whole feed on every filter and search
// keystroke and re-bound listeners would go with it.
(function () {
  var STEP = 290; // one chip (--chip-width) plus its gap

  function rowFor(button) {
    return button.parentElement.querySelector(".chips");
  }

  function sync(row) {
    var hidden = row.scrollWidth - row.clientWidth <= 1;
    var end = row.scrollWidth - row.clientWidth - 1;
    var buttons = row.parentElement.querySelectorAll(".chip-scroll");
    for (var index = 0; index < buttons.length; index++) {
      var button = buttons[index];
      button.hidden = hidden;
      button.disabled =
        button.getAttribute("data-scroll") === "-1"
          ? row.scrollLeft <= 0
          : row.scrollLeft >= end;
    }
  }

  function syncAll() {
    var rows = document.querySelectorAll(".chips");
    for (var index = 0; index < rows.length; index++) sync(rows[index]);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest(".chip-scroll");
    if (!button) return;
    var row = rowFor(button);
    if (row) row.scrollBy({ left: Number(button.getAttribute("data-scroll")) * STEP, behavior: "smooth" });
  });

  // `scroll` doesn't bubble, so it is caught on the way down instead.
  document.addEventListener(
    "scroll",
    function (event) {
      var row = event.target;
      if (row && row.classList && row.classList.contains("chips")) sync(row);
    },
    true
  );

  document.body.addEventListener("htmx:afterSwap", syncAll);
  window.addEventListener("resize", syncAll);
  syncAll(); // the script is deferred, so the feed is already parsed
})();
