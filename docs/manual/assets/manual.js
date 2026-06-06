/* HERD manual: interactions: nav active state, copy buttons,
   back-to-top, mobile nav, smooth anchor offset. */
(function () {
  // ----- copy buttons on .cmd blocks -----
  document.querySelectorAll(".cmd").forEach(function (block) {
    var btn = document.createElement("button");
    btn.className = "copy";
    btn.type = "button";
    btn.textContent = "Copy";
    btn.addEventListener("click", function () {
      var code = block.querySelector("code");
      var text = code ? code.innerText : block.innerText;
      navigator.clipboard && navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "Copied";
        btn.classList.add("done");
        setTimeout(function () { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1600);
      });
    });
    block.appendChild(btn);
  });

  // ----- back to top -----
  var totop = document.getElementById("totop");
  if (totop) {
    window.addEventListener("scroll", function () {
      totop.classList.toggle("show", window.scrollY > 520);
    }, { passive: true });
    totop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  // ----- mobile nav toggle -----
  var tgl = document.querySelector(".navtoggle");
  var nav = document.querySelector(".manualbar nav");
  if (tgl && nav) tgl.addEventListener("click", function () { nav.classList.toggle("open"); });

  // ----- sidebar scrollspy -----
  var links = Array.prototype.slice.call(document.querySelectorAll(".side a[href^='#']"));
  if (links.length) {
    var targets = links.map(function (a) {
      var el = document.getElementById(a.getAttribute("href").slice(1));
      return el ? { a: a, el: el } : null;
    }).filter(Boolean);
    var spy = function () {
      var y = window.scrollY + 110, cur = targets[0];
      targets.forEach(function (t) { if (t.el.offsetTop <= y) cur = t; });
      links.forEach(function (a) { a.classList.remove("active"); });
      if (cur) cur.a.classList.add("active");
    };
    window.addEventListener("scroll", spy, { passive: true });
    spy();
  }
})();
