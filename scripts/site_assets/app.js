// SIGNALPOST index page: pure client-side filter/sort over server-rendered <tr> rows.
// No fetch() of a separate data file - avoids the file:// CORS restriction on local JSON loads,
// so the page works identically opened directly from disk or served over GitHub Pages.
(function () {
  "use strict";
  var searchInput = document.getElementById("search");
  var tbody = document.querySelector("#companies tbody");
  var emptyState = document.getElementById("empty-state");
  if (!tbody) return;
  var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));

  function normalize(text) {
    return (text || "").toLowerCase();
  }

  function applyFilter() {
    var query = normalize(searchInput.value);
    var visibleCount = 0;
    rows.forEach(function (row) {
      var haystack = normalize(row.dataset.search);
      var match = query === "" || haystack.indexOf(query) !== -1;
      row.hidden = !match;
      if (match) visibleCount += 1;
    });
    if (emptyState) emptyState.hidden = visibleCount > 0;
  }

  if (searchInput) {
    searchInput.addEventListener("input", applyFilter);
  }

  var sortState = { key: null, direction: 1 };
  document.querySelectorAll("thead th[data-sort]").forEach(function (th) {
    th.tabIndex = 0;
    var activate = function () {
      var key = th.dataset.sort;
      sortState.direction = sortState.key === key ? -sortState.direction : 1;
      sortState.key = key;
      var sorted = rows.slice().sort(function (a, b) {
        var av = a.dataset[key] || "";
        var bv = b.dataset[key] || "";
        var an = parseFloat(av);
        var bn = parseFloat(bv);
        var cmp;
        if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") {
          cmp = an - bn;
        } else {
          cmp = av.localeCompare(bv);
        }
        return cmp * sortState.direction;
      });
      sorted.forEach(function (row) { tbody.appendChild(row); });
    };
    th.addEventListener("click", activate);
    th.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
  });

  applyFilter();
})();
