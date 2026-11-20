// Plays a Command Bridge canvas animation (.anim.html) outside Command Bridge.
// Each frame is a pure function of time: it sets window.width/height/duration and window.seek(t),
// and runs no timers of its own. On the canvas, Command Bridge drives seek(); here this file does.
// A frame plays once from the start when it comes into view, then holds its last state,
// the way the canvas plays it when his view lands on it.
(function () {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function fit(box, iframe, w, h) {
    const s = box.clientWidth / w;
    iframe.style.transform = 'scale(' + s + ')';
    box.style.height = Math.round(h * s) + 'px';
  }

  // Mount a frame into box; returns {play} once loaded (play restarts it from 0).
  function mount(box, src, onReady) {
    const iframe = document.createElement('iframe');
    iframe.src = src;
    iframe.title = box.dataset.title || 'Animation';
    iframe.setAttribute('scrolling', 'no');
    iframe.setAttribute('tabindex', '-1');
    // A deck remounts into the same box: stop the previous frame's loop before it runs on a detached window.
    if (box._stop) box._stop();
    box.appendChild(iframe);
    let raf = 0;
    box._stop = function () { cancelAnimationFrame(raf); };
    iframe.addEventListener('load', function () {
      const w = iframe.contentWindow;
      const W = w.width || 1280, H = w.height || 720, D = w.duration || 0;
      iframe.width = W; iframe.height = H;
      box.style.aspectRatio = W + ' / ' + H;
      fit(box, iframe, W, H);
      // One observer per box: a deck remounts into the same box, and an old observer
      // would keep sizing it to the previous slide's shape.
      if (box._ro) box._ro.disconnect();
      box._ro = new ResizeObserver(function () { fit(box, iframe, W, H); });
      box._ro.observe(box);
      function play() {
        cancelAnimationFrame(raf);
        if (reduce || !D) { w.seek(D); return; }
        const t0 = performance.now();
        (function tick(now) {
          const t = Math.min((now - t0) / 1000, D);
          w.seek(t);
          if (t < D) raf = requestAnimationFrame(tick);
        })(t0);
      }
      w.seek(0);
      onReady({ play: play, size: [W, H] });
    });
  }

  // Inline frames on a page: <div class="frame" data-src="..."> plays when scrolled into view.
  function inline() {
    const io = new IntersectionObserver(function (entries) {
      for (const e of entries) {
        if (e.isIntersecting && e.target._player && !e.target._played) {
          e.target._played = true;
          e.target._player.play();
        }
      }
    }, { threshold: 0.5 });
    document.querySelectorAll('.frame[data-src]').forEach(function (box) {
      mount(box, box.dataset.src, function (p) {
        box._player = p;
        io.observe(box);
        const btn = box.parentElement.querySelector('.replay');
        if (btn) btn.addEventListener('click', function () { p.play(); });
      });
    });
  }

  window.CBPlay = { mount: mount };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', inline);
  else inline();
})();
