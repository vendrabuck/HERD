/* HERD manual: annotation + canvas-edge engine.
   Measures the LIVE DOM so rings, pins and arrows stay locked to
   elements regardless of viewport size or font reflow.

   Annotations (declare on any element inside a .shot-stage):
     data-anno="1"                 number to show
     data-anno-shape="rect|ellipse" ring shape (default rect)
     data-anno-color="red|blue"    (default red)
     data-anno-pad="8"             ring padding px (default 7)
     data-anno-pin="tr|tl|br|bl|t|b|l|r"  pin corner (default tr)
     data-anno-arrow               (presence) draw arrow pin->element
     data-anno-noring              (presence) pin/arrow only, no ring

   Canvas edges (inside a .hd-canvas):
     <script type="application/json" class="hd-edges">
       [{ "from":"#a", "to":"#b", "layer":"l2", "status":"ok" }]
     </script>
   layer: l1|l2|l3 ; status: ok|nopath|none (overrides color red when nopath)
*/
(function () {
  var COLORS = { red: "#dc2626", blue: "#2563eb" };
  var LAYER = { l1: "#9ca3af", l2: "#3b82f6", l3: "#22c55e" };

  function svgEl(tag, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function rel(target, host) {
    var t = target.getBoundingClientRect(), h = host.getBoundingClientRect();
    return { x: t.left - h.left, y: t.top - h.top, w: t.width, h: t.height,
             cx: t.left - h.left + t.width / 2, cy: t.top - h.top + t.height / 2 };
  }

  // ---------- canvas edges ----------
  function drawEdges(canvas) {
    var cfgNode = canvas.querySelector("script.hd-edges");
    if (!cfgNode) return;
    var edges;
    try { edges = JSON.parse(cfgNode.textContent); } catch (e) { return; }
    var old = canvas.querySelector("svg.hd-edge-layer");
    if (old) old.remove();
    var W = canvas.clientWidth, H = canvas.clientHeight;
    var svg = svgEl("svg", { class: "hd-edge-layer", width: W, height: H,
      viewBox: "0 0 " + W + " " + H });
    svg.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:1;";

    edges.forEach(function (ed) {
      var a = canvas.querySelector(ed.from), b = canvas.querySelector(ed.to);
      if (!a || !b) return;
      var ra = rel(a, canvas), rb = rel(b, canvas);
      // choose anchor sides by dominant axis
      var dx = rb.cx - ra.cx, dy = rb.cy - ra.cy, p1, p2, c1, c2;
      if (Math.abs(dx) >= Math.abs(dy)) {
        var s = dx >= 0 ? 1 : -1;
        p1 = { x: ra.cx + s * ra.w / 2, y: ra.cy };
        p2 = { x: rb.cx - s * rb.w / 2, y: rb.cy };
        var mx = (p1.x + p2.x) / 2; c1 = { x: mx, y: p1.y }; c2 = { x: mx, y: p2.y };
      } else {
        var sv = dy >= 0 ? 1 : -1;
        p1 = { x: ra.cx, y: ra.cy + sv * ra.h / 2 };
        p2 = { x: rb.cx, y: rb.cy - sv * rb.h / 2 };
        var my = (p1.y + p2.y) / 2; c1 = { x: p1.x, y: my }; c2 = { x: p2.x, y: my };
      }
      var color = ed.status === "nopath" ? "#ef4444" : (LAYER[ed.layer] || "#9ca3af");
      var dash = ed.layer === "l1" ? "7 4" : (ed.layer === "l3" ? "3 4" : (ed.status === "nopath" ? "7 5" : null));
      var d = "M" + p1.x + "," + p1.y + " C" + c1.x + "," + c1.y + " " + c2.x + "," + c2.y + " " + p2.x + "," + p2.y;
      var path = svgEl("path", { d: d, fill: "none", stroke: color, "stroke-width": ed.status === "nopath" ? 2.5 : 2 });
      if (dash) path.setAttribute("stroke-dasharray", dash);
      if (ed.status === "nopath") path.setAttribute("opacity", "0.95");
      svg.appendChild(path);
    });
    canvas.insertBefore(svg, canvas.firstChild);
  }

  // ---------- annotations ----------
  function pinPos(r, corner, off) {
    switch (corner) {
      case "tl": return { x: r.x - off, y: r.y - off, ax: r.x, ay: r.y };
      case "tr": return { x: r.x + r.w + off, y: r.y - off, ax: r.x + r.w, ay: r.y };
      case "bl": return { x: r.x - off, y: r.y + r.h + off, ax: r.x, ay: r.y + r.h };
      case "br": return { x: r.x + r.w + off, y: r.y + r.h + off, ax: r.x + r.w, ay: r.y + r.h };
      case "t":  return { x: r.cx, y: r.y - off, ax: r.cx, ay: r.y };
      case "b":  return { x: r.cx, y: r.y + r.h + off, ax: r.cx, ay: r.y + r.h };
      case "l":  return { x: r.x - off, y: r.cy, ax: r.x, ay: r.cy };
      case "r":  return { x: r.x + r.w + off, y: r.cy, ax: r.x + r.w, ay: r.cy };
      default:   return { x: r.x + r.w + off, y: r.y - off, ax: r.x + r.w, ay: r.y };
    }
  }

  function annotate(stage) {
    var old = stage.querySelector("svg.anno-layer");
    if (old) old.remove();
    stage.querySelectorAll(".anno-pin").forEach(function (p) { p.remove(); });

    var marks = stage.querySelectorAll("[data-anno]");
    if (!marks.length) return;
    var W = stage.clientWidth, H = stage.clientHeight;
    var svg = svgEl("svg", { class: "anno-layer", width: W, height: H, viewBox: "0 0 " + W + " " + H });
    svg.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:20;overflow:visible;";

    marks.forEach(function (el) {
      var n = el.getAttribute("data-anno");
      var color = COLORS[el.getAttribute("data-anno-color")] || COLORS.red;
      var pad = parseFloat(el.getAttribute("data-anno-pad") || "7");
      var shape = el.getAttribute("data-anno-shape") || "rect";
      var corner = el.getAttribute("data-anno-pin") || "tr";
      var r = rel(el, stage);

      if (el.getAttribute("data-anno-noring") === null && !el.hasAttribute("data-anno-noring")) {
        if (shape === "ellipse") {
          svg.appendChild(svgEl("ellipse", {
            cx: r.cx, cy: r.cy, rx: r.w / 2 + pad + 4, ry: r.h / 2 + pad,
            fill: "none", stroke: color, "stroke-width": 2.5
          }));
        } else {
          var rad = Math.min(12, r.h / 2 + pad);
          svg.appendChild(svgEl("rect", {
            x: r.x - pad, y: r.y - pad, width: r.w + pad * 2, height: r.h + pad * 2,
            rx: rad, ry: rad, fill: "none", stroke: color, "stroke-width": 2.5
          }));
        }
      }

      var off = 17;
      var pp = pinPos({ x: r.x - pad, y: r.y - pad, w: r.w + pad * 2, h: r.h + pad * 2,
                        cx: r.cx, cy: r.cy }, corner, off);
      // clamp pin into stage
      pp.x = Math.max(13, Math.min(W - 13, pp.x));
      pp.y = Math.max(13, Math.min(H - 13, pp.y));

      if (el.hasAttribute("data-anno-arrow")) {
        var line = svgEl("path", {
          d: "M" + pp.x + "," + pp.y + " L" + pp.ax + "," + pp.ay,
          stroke: color, "stroke-width": 2.5, fill: "none"
        });
        svg.appendChild(line);
        // arrowhead
        var ang = Math.atan2(pp.ay - pp.y, pp.ax - pp.x);
        var ah = 8;
        var p = pp.ax + "," + pp.ay
          + " " + (pp.ax - ah * Math.cos(ang - 0.5)) + "," + (pp.ay - ah * Math.sin(ang - 0.5))
          + " " + (pp.ax - ah * Math.cos(ang + 0.5)) + "," + (pp.ay - ah * Math.sin(ang + 0.5));
        svg.appendChild(svgEl("polygon", { points: p, fill: color }));
      }

      var pin = document.createElement("span");
      pin.className = "anno-pin";
      pin.textContent = n;
      pin.style.cssText = "position:absolute;left:" + pp.x + "px;top:" + pp.y + "px;"
        + "transform:translate(-50%,-50%);width:25px;height:25px;border-radius:50%;"
        + "background:" + color + ";color:#fff;font:700 13px/1 var(--font-sans);"
        + "display:flex;align-items:center;justify-content:center;z-index:21;"
        + "box-shadow:0 0 0 2.5px #fff,0 1px 4px rgba(0,0,0,.35);font-variant-numeric:tabular-nums;";
      stage.appendChild(pin);
    });

    stage.insertBefore(svg, stage.firstChild);
  }

  function redrawAll() {
    document.querySelectorAll(".hd-canvas").forEach(drawEdges);
    document.querySelectorAll(".shot-stage").forEach(annotate);
  }

  var t;
  function schedule() { clearTimeout(t); t = setTimeout(redrawAll, 60); }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", redrawAll);
  else redrawAll();
  window.addEventListener("load", redrawAll);
  window.addEventListener("resize", schedule);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(redrawAll);
  // observe each stage/canvas for size changes (collapsibles, image load)
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(schedule);
    document.querySelectorAll(".shot-stage, .hd-canvas").forEach(function (n) { ro.observe(n); });
  }
  // expose for manual re-trigger (e.g. when an <details> opens)
  window.HERDannotate = redrawAll;
  document.addEventListener("toggle", function (e) {
    if (e.target && e.target.tagName === "DETAILS") schedule();
  }, true);
})();
