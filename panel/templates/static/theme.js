/* Header theme picker: applies immediately and remembers the choice in
   localStorage ("fakhra-theme"); base.html's boot script replays it
   before paint on every page load. No backend — preference is per-browser. */
(function () {
  "use strict";
  var picker = document.getElementById("theme-picker");
  if (!picker) return;
  try {
    var saved = localStorage.getItem("fakhra-theme");
    if (saved) picker.value = saved;
  } catch (e) {}
  picker.addEventListener("change", function () {
    try {
      localStorage.setItem("fakhra-theme", picker.value);
    } catch (e) {}
    document.documentElement.setAttribute("data-theme", picker.value);
  });
})();
